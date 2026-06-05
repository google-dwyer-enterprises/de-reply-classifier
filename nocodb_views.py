"""NocoDB view management for per-batch review URLs.

The Lead Scrape Automation worker calls create_review_view() at mark_ready
time to create a grid view on prospeo_new_leads scoped to a specific
scrape_request_id. The email button in notifier.py links to that view's
public share URL, so Jam lands inside a view that ONLY contains her
batch's leads — header-checkbox / multi-select / bulk-edit can't reach
other batches' rows because they don't match the view's filter.

Best-effort: if NocoDB is unreachable, the API token is missing, or any
single step fails, returns (None, None, None) and the worker falls back
to the prior NOCODB_ROW_URL_TEMPLATE link. The email still sends; the
worker still completes the scrape; Jam just sees the old generic link.

Env vars (all set on the Railway service):
  NOCODB_URL                       — root, e.g. https://vd-master-leads.up.railway.app
  NOCODB_API_TOKEN                 — `nc_pat_...` token with column + view scope
  NOCODB_PROSPEO_TABLE_ID          — table id for prospeo_new_leads
  NOCODB_LEAD_APPROVAL_COL_ID      — column id for lead_approval (filter)
  NOCODB_SCRAPE_REQUEST_ID_COL_ID  — column id for scrape_request_id (filter)
"""

from __future__ import annotations

import os
from typing import Any

import requests


_TIMEOUT_S = 30


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _base_url() -> str:
    return _env("NOCODB_URL").rstrip("/")


def _headers() -> dict[str, str]:
    return {
        "xc-token": _env("NOCODB_API_TOKEN"),
        "Content-Type": "application/json",
    }


def _is_configured() -> bool:
    return bool(_base_url() and _env("NOCODB_API_TOKEN"))


def create_review_view(
    scrape_request_id: int,
    *,
    on_log=None,
) -> tuple[str | None, str | None, str | None]:
    """Create a per-batch grid view scoped to scrape_request_id = N.

    Returns (view_id, share_uuid, share_url) on success, or (None, None, None)
    on any failure (missing config, NocoDB down, API rejection, etc.).
    Failures are non-fatal — the worker continues without per-batch isolation.

    The view filters by:
      - scrape_request_id = N
      - lead_approval = 'pending'
    so Jam sees only her batch's undecided leads. Approved/rejected rows
    disappear from the view as she works (because they no longer match
    lead_approval='pending') — exactly the behaviour of the global Review
    batch view, but scoped to a single batch.
    """
    log = on_log or (lambda _msg: None)

    if not _is_configured():
        log("nocodb_views: not configured (NOCODB_URL or NOCODB_API_TOKEN missing); skipping")
        return None, None, None

    base = _base_url()
    table_id = _env("NOCODB_PROSPEO_TABLE_ID")
    lead_approval_cid = _env("NOCODB_LEAD_APPROVAL_COL_ID")
    scrape_request_id_cid = _env("NOCODB_SCRAPE_REQUEST_ID_COL_ID")

    if not (table_id and lead_approval_cid and scrape_request_id_cid):
        log("nocodb_views: column/table ids not configured; skipping")
        return None, None, None

    try:
        # 1. Create the grid view
        r = requests.post(
            f"{base}/api/v2/meta/tables/{table_id}/grids",
            headers=_headers(),
            json={"title": f"Review batch #{scrape_request_id}"},
            timeout=_TIMEOUT_S,
        )
        if not r.ok:
            log(f"nocodb_views: create grid failed ({r.status_code}): {r.text[:200]}")
            return None, None, None
        view = r.json()
        view_id = view.get("id") or (view.get("data") or {}).get("id")
        if not view_id:
            log(f"nocodb_views: grid created but no id in response: {view}")
            return None, None, None

        # 2. Add filter: scrape_request_id = N
        rf = requests.post(
            f"{base}/api/v2/meta/views/{view_id}/filters",
            headers=_headers(),
            json={
                "fk_column_id": scrape_request_id_cid,
                "comparison_op": "eq",
                "value": str(scrape_request_id),
                "logical_op": "and",
            },
            timeout=_TIMEOUT_S,
        )
        if not rf.ok:
            log(f"nocodb_views: filter on scrape_request_id failed ({rf.status_code}): {rf.text[:200]}")
            # Best-effort: keep view alive; without filter it's a global view
            # which is worse than the old behaviour. Tear it down to be safe.
            _delete_view_quiet(base, view_id)
            return None, None, None

        # 3. Add filter: lead_approval = 'pending'
        rf2 = requests.post(
            f"{base}/api/v2/meta/views/{view_id}/filters",
            headers=_headers(),
            json={
                "fk_column_id": lead_approval_cid,
                "comparison_op": "eq",
                "value": "pending",
                "logical_op": "and",
            },
            timeout=_TIMEOUT_S,
        )
        if not rf2.ok:
            log(f"nocodb_views: filter on lead_approval failed ({rf2.status_code}): {rf2.text[:200]}")
            # Not fatal — view still scoped to the batch, just shows all
            # decisions including the approved/rejected leads. Continue.

        # 4. Enable public share
        rs = requests.post(
            f"{base}/api/v2/meta/views/{view_id}/share",
            headers=_headers(),
            json={},
            timeout=_TIMEOUT_S,
        )
        share_uuid: str | None = None
        if rs.ok:
            share_uuid = rs.json().get("uuid") or (rs.json().get("data") or {}).get("uuid")
        if not share_uuid:
            log(f"nocodb_views: share creation failed ({rs.status_code}): {rs.text[:200]}")
            return view_id, None, None

        share_url = f"{base}/nc/view/{share_uuid}"
        log(f"nocodb_views: created Review batch #{scrape_request_id} (view={view_id}, share={share_uuid})")
        return view_id, share_uuid, share_url

    except Exception as e:  # noqa: BLE001 — best-effort: any failure -> fall back
        log(f"nocodb_views: unexpected error during create_review_view: {e}")
        return None, None, None


def _delete_view_quiet(base: str, view_id: str) -> None:
    try:
        requests.delete(f"{base}/api/v2/meta/views/{view_id}",
                        headers=_headers(), timeout=_TIMEOUT_S)
    except Exception:
        pass


def delete_review_view(view_id: str, *, on_log=None) -> bool:
    """Best-effort delete of a previously-created per-batch view. Used when
    a batch finalizes and we want to keep the NocoDB sidebar tidy.
    """
    log = on_log or (lambda _msg: None)
    if not _is_configured() or not view_id:
        return False
    try:
        r = requests.delete(
            f"{_base_url()}/api/v2/meta/views/{view_id}",
            headers=_headers(),
            timeout=_TIMEOUT_S,
        )
        if r.ok:
            log(f"nocodb_views: deleted view {view_id}")
            return True
        log(f"nocodb_views: delete view {view_id} failed ({r.status_code})")
        return False
    except Exception as e:  # noqa: BLE001
        log(f"nocodb_views: delete view {view_id} errored: {e}")
        return False
