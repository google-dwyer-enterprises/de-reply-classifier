"""Trigger NocoDB metadata sync via API.

Picks up new tables / materialized views / column changes from the underlying
Postgres so they appear in the NocoDB UI.

Usage (credentials passed via env to avoid committing them):

    $env:NOCODB_URL = "https://your-nocodb.example.com"
    $env:NOCODB_EMAIL = "you@example.com"
    $env:NOCODB_PASSWORD = "..."
    python scripts/nocodb_sync.py

The script will:
  1. Log in with email/password (gets a JWT).
  2. List bases, pick the one matching $env:NOCODB_BASE (or the first one).
  3. List sources in that base, pick the Postgres source.
  4. Trigger metadata sync.
"""

from __future__ import annotations

import json
import os
import sys

import requests


def get(url: str, **kw):
    r = requests.get(url, timeout=30, **kw)
    if r.status_code >= 400:
        print(f"  ! GET {url} -> {r.status_code}: {r.text[:300]}", file=sys.stderr)
    return r


def post(url: str, **kw):
    r = requests.post(url, timeout=60, **kw)
    if r.status_code >= 400:
        print(f"  ! POST {url} -> {r.status_code}: {r.text[:300]}", file=sys.stderr)
    return r


def main() -> None:
    base_url = os.environ.get("NOCODB_URL", "").rstrip("/")
    email = os.environ.get("NOCODB_EMAIL", "").strip()
    password = os.environ.get("NOCODB_PASSWORD", "").strip()
    base_name_filter = os.environ.get("NOCODB_BASE", "").strip().lower()

    if not (base_url and email and password):
        sys.exit("FATAL: set NOCODB_URL, NOCODB_EMAIL, NOCODB_PASSWORD env vars")

    print(f"NocoDB at {base_url}")

    # ----- Step 1: sign in -----
    print("Signing in...")
    r = post(f"{base_url}/api/v1/auth/user/signin",
             json={"email": email, "password": password})
    if not r.ok:
        # Try v2 endpoint as a fallback
        r = post(f"{base_url}/api/v2/auth/signin",
                 json={"email": email, "password": password})
    r.raise_for_status()
    token = r.json().get("token")
    if not token:
        sys.exit(f"FATAL: signin response missing token: {r.json()}")
    headers = {"xc-auth": token, "Content-Type": "application/json"}
    print("  [OK] signed in")

    # ----- Step 2: list bases -----
    # Try v2 first, then v1.
    print("\nListing bases...")
    bases_resp = get(f"{base_url}/api/v2/meta/bases", headers=headers)
    if not bases_resp.ok:
        bases_resp = get(f"{base_url}/api/v1/db/meta/projects/", headers=headers)
    bases_resp.raise_for_status()
    bases_payload = bases_resp.json()
    bases = bases_payload.get("list") or bases_payload.get("data") or bases_payload
    if not isinstance(bases, list):
        bases = [bases]
    for b in bases:
        tag = "(target)" if (base_name_filter and base_name_filter in (b.get("title") or "").lower()) else ""
        print(f"  - {b.get('id')!r}  {b.get('title')!r} {tag}")

    target_base = None
    if base_name_filter:
        target_base = next(
            (b for b in bases if base_name_filter in (b.get("title") or "").lower()),
            None,
        )
    if not target_base:
        # Default to the first base if we can't filter
        target_base = bases[0] if bases else None
        if not target_base:
            sys.exit("FATAL: no bases found")
        print(f"  No NOCODB_BASE filter set or matched; defaulting to first: {target_base.get('title')!r}")

    base_id = target_base.get("id")
    print(f"\nUsing base: {target_base.get('title')!r}  (id={base_id})")

    # ----- Step 3: list sources -----
    print("Listing sources in base...")
    sources_resp = get(f"{base_url}/api/v2/meta/bases/{base_id}/sources", headers=headers)
    if not sources_resp.ok:
        sources_resp = get(
            f"{base_url}/api/v1/db/meta/projects/{base_id}/bases", headers=headers
        )
    sources_resp.raise_for_status()
    src_payload = sources_resp.json()
    sources = src_payload.get("list") or src_payload.get("data") or src_payload
    if not isinstance(sources, list):
        sources = [sources]
    for s in sources:
        print(f"  - id={s.get('id')!r}  alias={s.get('alias')!r}  type={s.get('type')!r}")

    # Pick the source matching $env:NOCODB_SOURCE (preferred) or the first
    # Postgres source, or the first non-meta source.
    source_alias_filter = os.environ.get("NOCODB_SOURCE", "").strip().lower()
    pg_source = None
    if source_alias_filter:
        pg_source = next(
            (s for s in sources
             if source_alias_filter in (s.get("alias") or "").lower()),
            None,
        )
    if not pg_source:
        pg_source = next(
            (s for s in sources if (s.get("type") or "").lower() == "pg"),
            None,
        )
    if not pg_source:
        pg_source = next((s for s in sources if not s.get("is_meta")), None)
    if not pg_source:
        pg_source = sources[0] if sources else None
    if not pg_source:
        sys.exit("FATAL: no source to sync")
    source_id = pg_source.get("id")
    print(f"\nUsing source: alias={pg_source.get('alias')!r}  (id={source_id})")

    # ----- Step 4: meta-diff (preview) -----
    print("\nFetching meta-diff (preview of pending changes)...")
    diff_url_v2 = f"{base_url}/api/v2/meta/bases/{base_id}/sources/{source_id}/meta-diff"
    diff_url_v1 = f"{base_url}/api/v1/db/meta/projects/{base_id}/meta-diff/{source_id}"
    diff_resp = get(diff_url_v2, headers=headers)
    if not diff_resp.ok:
        diff_resp = get(diff_url_v1, headers=headers)
    if diff_resp.ok:
        diff = diff_resp.json()
        diff_list = diff.get("list") or diff.get("data") or diff
        if isinstance(diff_list, list):
            for table in diff_list:
                detected = table.get("detectedChanges") or []
                if detected:
                    print(f"  {table.get('table_name')!r}: {len(detected)} changes")
                    for c in detected:
                        print(f"    - {c.get('msg') or c}")
        else:
            print(f"  diff response: {json.dumps(diff, indent=2)[:500]}")
    else:
        print("  (meta-diff endpoint not available; will attempt direct sync)")

    # ----- Step 5: apply sync -----
    print("\nApplying meta-sync...")
    sync_url_v2 = f"{base_url}/api/v2/meta/bases/{base_id}/sources/{source_id}/meta-diff"
    sync_url_v1 = f"{base_url}/api/v1/db/meta/projects/{base_id}/meta-diff/{source_id}"
    r = post(sync_url_v2, headers=headers)
    if not r.ok:
        r = post(sync_url_v1, headers=headers)
    if not r.ok:
        # Some versions use a separate "sync" route
        for alt in [
            f"{base_url}/api/v2/meta/bases/{base_id}/sources/{source_id}/sync",
            f"{base_url}/api/v1/db/meta/projects/{base_id}/meta-diff/{source_id}/sync",
        ]:
            r = post(alt, headers=headers)
            if r.ok:
                print(f"  [OK] sync succeeded at {alt}")
                break
    r.raise_for_status()
    print("  [OK] meta-sync applied")

    # ----- Step 6: re-fetch table list to verify -----
    print("\nVerifying — fetching tables in base after sync...")
    tables_url_v2 = f"{base_url}/api/v2/meta/bases/{base_id}/tables"
    tables_url_v1 = f"{base_url}/api/v1/db/meta/projects/{base_id}/tables"
    t_resp = get(tables_url_v2, headers=headers)
    if not t_resp.ok:
        t_resp = get(tables_url_v1, headers=headers)
    if t_resp.ok:
        tables = (t_resp.json().get("list") or t_resp.json().get("data") or [])
        names = sorted([t.get("table_name") or t.get("title") for t in tables])
        for n in names:
            tag = " <-- NEW" if n in ("followup_tracker_mv", "lead_outcomes") else ""
            print(f"  - {n}{tag}")
        if "followup_tracker_mv" in names:
            print("\n[OK] followup_tracker_mv is registered in NocoDB.")
        else:
            print("\n[!] followup_tracker_mv NOT in the table list yet. "
                  "May need a manual UI refresh, or the MV's type isn't recognized by this NocoDB version.")
    else:
        print("  (could not list tables to verify)")


if __name__ == "__main__":
    main()
