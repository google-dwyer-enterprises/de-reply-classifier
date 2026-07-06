"""Phase 2: Instantly ETL — 7-day test window.

Pulls received replies from Instantly v2 API and upserts into Supabase
`replies`, skipping duplicates. Does NOT update sync_state (test run).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

import httpx
import requests
from dotenv import load_dotenv
from supabase import create_client

from config import extract_client

API_BASE = "https://api.instantly.ai/api/v2"
LIST_EMAILS_URL = f"{API_BASE}/emails"
LIST_CAMPAIGNS_URL = f"{API_BASE}/campaigns"
LIST_TAGS_URL = f"{API_BASE}/custom-tags"
LIST_TAG_MAPPINGS_URL = f"{API_BASE}/custom-tag-mappings"
LIST_LEAD_LABELS_URL = f"{API_BASE}/lead-labels"
LOOKBACK_DAYS = 7
DEFAULT_MIN_INTERVAL_S = 1.0
MAX_RETRIES = 8
PAGE_LIMIT = 100
INSERT_BATCH = 200
FLUSH_EVERY_PAGES = 5
DEBUG_DIR = Path(__file__).parent / "debug"

# Email-type dispatch (FOLLOWUP_ANALYSIS_PLAN.md Phase 2 Edit A).
# Inbound goes to `replies`, outbound to `sent_messages`. Their timestamp
# columns are differently named.
TABLE_BY_TYPE = {"received": "replies", "sent": "sent_messages"}
TS_COL_BY_TYPE = {"received": "reply_timestamp", "sent": "sent_timestamp"}


# --------------------------------------------------------------------------- #
# Rate limit + retry
# --------------------------------------------------------------------------- #

class RateLimiter:
    def __init__(self, default_interval: float):
        self.default_interval = default_interval
        self.next_allowed_at = 0.0

    def wait(self) -> None:
        delay = self.next_allowed_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)

    def update_from_headers(self, headers) -> None:
        remaining = headers.get("x-ratelimit-remaining")
        reset = headers.get("x-ratelimit-reset")
        now = time.monotonic()
        if remaining is not None and reset is not None:
            try:
                remaining_i = int(remaining)
                reset_s = float(reset)
                if remaining_i <= 0:
                    self.next_allowed_at = now + max(reset_s, self.default_interval)
                    return
                per_call = reset_s / max(remaining_i, 1)
                self.next_allowed_at = now + max(per_call, 0.0)
                return
            except (TypeError, ValueError):
                pass
        self.next_allowed_at = now + self.default_interval


def request_with_backoff(
    session: requests.Session,
    url: str,
    params: dict,
    limiter: RateLimiter,
) -> requests.Response:
    """GET with retry on:
      - HTTP 429 (rate-limited) — back off per retry-after or exponential
      - HTTP 5xx                 — exponential backoff
      - transport errors         — ConnectionError, Timeout, ChunkedEncodingError
        (SSL handshake reset, socket reset, DNS hiccup, etc.) — exponential

    The transport-error branch is necessary because long syncs against the
    Instantly API occasionally hit WinError 10054 ("connection forcibly closed
    by the remote host") on SSL handshake. Without retry, one transient
    socket reset kills the whole backfill.
    """
    # Instantly's first-page response on wide windows (e.g. 90 days) routinely
    # exceeds 30s — known issue. Use a longer per-request timeout for the
    # paginated endpoints.
    REQUEST_TIMEOUT_S = 120
    for attempt in range(MAX_RETRIES):
        limiter.wait()
        try:
            resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT_S)
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.ChunkedEncodingError) as e:
            sleep_s = min(60.0, (2 ** attempt) + random.random())
            print(f"  {type(e).__name__} ({str(e)[:120]}); "
                  f"retry {attempt + 1}/{MAX_RETRIES} in {sleep_s:.1f}s",
                  file=sys.stderr)
            time.sleep(sleep_s)
            continue
        limiter.update_from_headers(resp.headers)

        if resp.status_code == 429:
            retry_after = resp.headers.get("retry-after")
            if retry_after:
                sleep_s = float(retry_after)
            else:
                sleep_s = min(60.0, max(15.0, (2 ** attempt) + random.random()))
            print(f"  429; backing off {sleep_s:.1f}s (attempt {attempt + 1}/{MAX_RETRIES})")
            time.sleep(sleep_s)
            continue

        if 500 <= resp.status_code < 600:
            sleep_s = (2 ** attempt) + random.random()
            print(f"  {resp.status_code}; retrying in {sleep_s:.1f}s")
            time.sleep(sleep_s)
            continue

        resp.raise_for_status()
        return resp

    sys.exit(f"FATAL: exhausted {MAX_RETRIES} retries on {url}")


# --------------------------------------------------------------------------- #
# Env + Supabase
# --------------------------------------------------------------------------- #

def get_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        sys.exit(f"FATAL: {name} is not set in .env")
    return val


def verify_supabase(client, email_type: str = "received", retries: int = 4) -> bool:
    """Cheap, resilient reachability check for this sync's destination table.

    Root-cause history (2026-07-06 cron failure): this used
    `select("*", count="exact", head=True)`. An EXACT count(*) on a large table
    (sent_messages ~430k rows) periodically exceeds Supabase's statement timeout
    and PostgREST returns `500 "JSON could not be generated"` (empty details) —
    which sys.exit()'d and, via the `&&` cron chain, aborted the ENTIRE daily
    refresh (sync + classify + follow-up steps).

    Fix (layered):
      1. Reachability is a `limit(1)` read — it cannot time out like a full
         count scan. The rowcount is fetched separately as an ESTIMATE
         (pg_class reltuples, O(1)), best-effort and purely informational.
      2. Transient errors are retried with backoff.
      3. The RECEIVED table is the critical path -> sys.exit() on exhaustion.
         The SENT table only feeds the follow-up tracker and is idempotent, so
         a transient blip returns False (caller skips this run's sent pass and
         the pipeline continues) instead of killing the whole cron.

    Returns True if reachable, False if a non-critical (sent) check gave up.
    """
    table = TABLE_BY_TYPE[email_type]
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            client.table(table).select("*").limit(1).execute()   # can't time out
            try:
                r = client.table(table).select("*", count="estimated", head=True).execute()
                cnt = r.count if r.count is not None else "?"
            except Exception:
                cnt = "?"   # estimate is optional; reachability already proven
            print(f"Supabase OK — {table} table reachable (est. rowcount={cnt})")
            return True
        except Exception as e:
            last_err = e
            if attempt < retries:
                wait = min(30, 2 ** attempt)
                print(f"  Supabase check on {table} failed (attempt {attempt}/{retries}): "
                      f"{str(e)[:140]} — retrying in {wait}s")
                time.sleep(wait)
    if email_type == "received":
        sys.exit(f"FATAL: cannot access Supabase '{table}' table after {retries} "
                 f"attempts: {last_err}")
    print(f"WARNING: skipping the '{table}' sync this run — reachability check failed "
          f"after {retries} attempts: {last_err}. Non-critical (feeds the follow-up "
          f"tracker, catches up next run); the pipeline continues.")
    return False


def make_session(api_key: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    })
    return s


# --------------------------------------------------------------------------- #
# Pagination
# --------------------------------------------------------------------------- #

def extract_items(payload) -> tuple[list, str | None]:
    if isinstance(payload, list):
        return payload, None
    if not isinstance(payload, dict):
        return [], None
    items = None
    for key in ("items", "data", "results", "campaigns", "emails"):
        if key in payload and isinstance(payload[key], list):
            items = payload[key]
            break
    next_cursor = payload.get("next_starting_after") or payload.get("starting_after")
    return items or [], next_cursor


def paginate(
    session: requests.Session,
    url: str,
    base_params: dict,
    limiter: RateLimiter,
    label: str,
    start_cursor: str | None = None,
):
    cursor: str | None = start_cursor
    page = 0
    while True:
        page += 1
        params = dict(base_params)
        params["limit"] = PAGE_LIMIT
        if cursor:
            params["starting_after"] = cursor
        print(f"Fetching {label} page {page}" + (f" (cursor={cursor})" if cursor else ""))
        resp = request_with_backoff(session, url, params, limiter)
        payload = resp.json()
        items, next_cursor = extract_items(payload)

        advance_cursor: str | None = None
        if next_cursor and next_cursor != cursor:
            advance_cursor = next_cursor
        elif len(items) >= PAGE_LIMIT:
            last_id = items[-1].get("id") if items else None
            if last_id and last_id != cursor:
                advance_cursor = last_id

        yield page, cursor, advance_cursor, items

        if advance_cursor:
            cursor = advance_cursor
            continue
        return


def fetch_all_campaigns(session: requests.Session, limiter: RateLimiter) -> dict[str, str]:
    campaigns: dict[str, str] = {}
    for _page, _cur, _next, items in paginate(session, LIST_CAMPAIGNS_URL, {}, limiter, "campaigns"):
        for c in items:
            cid = c.get("id")
            if cid:
                campaigns[cid] = c.get("name") or ""
    return campaigns


def fetch_campaign_tags(
    session: requests.Session,
    limiter: RateLimiter,
) -> dict[str, list[str]]:
    """Returns {campaign_id: [tag_label, ...]}.

    Two passes: list custom tags (id -> label), then list tag-resource
    mappings and keep only campaign-typed resources.
    """
    tag_label: dict[str, str] = {}
    for _p, _c, _n, items in paginate(session, LIST_TAGS_URL, {}, limiter, "tags"):
        for t in items:
            tid = t.get("id")
            if tid:
                tag_label[tid] = t.get("label") or ""

    # Instantly encodes resource_type as int: 1 = email account, 2 = campaign.
    RESOURCE_TYPE_CAMPAIGN = 2

    by_campaign: dict[str, list[str]] = {}
    for _p, _c, _n, items in paginate(session, LIST_TAG_MAPPINGS_URL, {}, limiter, "tag-mappings"):
        for m in items:
            if m.get("resource_type") != RESOURCE_TYPE_CAMPAIGN:
                continue
            cid = m.get("resource_id")
            label = tag_label.get(m.get("tag_id"))
            if cid and label:
                by_campaign.setdefault(cid, []).append(label)

    return by_campaign


# Instantly's built-in interest_status codes (system-defined, not returned
# by /lead-labels). Custom labels from /lead-labels overlay these.
BUILTIN_LEAD_STATUSES: dict[int, str] = {
    0: "Lead",
    1: "Interested",
    2: "Meeting booked",
    3: "Meeting completed",
    4: "Won",
    -1: "Out of office",
    -2: "Not interested",
    -3: "Wrong person",
}


def fetch_lead_labels(
    session: requests.Session,
    limiter: RateLimiter,
) -> dict[int, str]:
    """Returns {interest_status_code: label}.

    Starts from BUILTIN_LEAD_STATUSES (system defaults) and overlays custom
    labels from /lead-labels. Re-run after adding new labels in Instantly.
    """
    labels: dict[int, str] = dict(BUILTIN_LEAD_STATUSES)
    for _p, _c, _n, items in paginate(session, LIST_LEAD_LABELS_URL, {}, limiter, "lead-labels"):
        for it in items:
            code = it.get("interest_status")
            label = it.get("label")
            if code is not None and label and label.strip():
                labels[int(code)] = label
    return labels


# --------------------------------------------------------------------------- #
# Email parsing
# --------------------------------------------------------------------------- #

class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            self._parts.append(data)

    def text(self) -> str:
        return re.sub(r"\s+", " ", "".join(self._parts)).strip()


def html_to_text(html: str) -> str:
    if not html:
        return ""
    p = _TextExtractor()
    try:
        p.feed(html)
    except Exception:
        return re.sub(r"<[^>]+>", " ", html).strip()
    return p.text()


def parse_email(
    email: dict,
    campaigns_map: dict[str, str],
    tags_map: dict[str, list[str]],
    lead_labels: dict[int, str],
    email_type: str = "received",
) -> dict:
    """Build a DB row from an Instantly email object.

    Direction-specific fields (per FOLLOWUP_ANALYSIS_PLAN.md Phase 2 Edit B):
      - received: lead_email <- from_address_email, timestamp <- reply_timestamp
      - sent:     lead_email <- email["lead"],     timestamp <- sent_timestamp
                  + outbound-only ue_type, step, in addition to thread_id

    Note: `send_kind` is a STORED generated column on sent_messages (Postgres
    derives it from ue_type + step). Do NOT include it here.

    Note: `in_reply_to_id` was in v1/v2 of the plan. Removed per Phase 0.6
    finding — Instantly's /v2/emails does not expose this field.
    """
    raw_from = email.get("from_address_email") or ""
    campaign_id = email.get("campaign_id")
    campaign_name = campaigns_map.get(campaign_id, "UNKNOWN") if campaign_id else "UNKNOWN"

    body_obj = email.get("body") or {}
    body_text = (body_obj.get("text") or "").strip()
    if not body_text:
        body_text = html_to_text(body_obj.get("html") or "")

    # Direction-specific identity + timestamp column name
    if email_type == "received":
        lead_email = raw_from.strip().lower()
        ts_col = "reply_timestamp"
    else:  # sent
        lead_email = (email.get("lead") or "").strip().lower()
        ts_col = "sent_timestamp"

    row = {
        "lead_email":           lead_email,
        "campaign_id":          campaign_id,
        "campaign_name":        campaign_name,
        "client":               extract_client(campaign_name),
        ts_col:                 email.get("timestamp_email"),
        "subject":              email.get("subject"),
        "body":                 body_text,
        "instantly_message_id": email.get("id"),
        "thread_id":            email.get("thread_id"),
        "tags":                 tags_map.get(campaign_id, []) if campaign_id else [],
        "lead_status_code":     email.get("i_status"),
        "lead_status":          lead_labels.get(email.get("i_status"))
                                if email.get("i_status") is not None else None,
    }

    # Outbound-only fields (sent_messages has these columns; replies doesn't)
    if email_type == "sent":
        row["ue_type"] = email.get("ue_type")
        row["step"]    = email.get("step")

    return row


# --------------------------------------------------------------------------- #
# Upsert
# --------------------------------------------------------------------------- #

def chunk(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


UPSERT_MAX_RETRIES = 6


def _upsert_batch_with_retry(supabase, table: str, batch: list[dict]) -> int:
    """Single batch upsert with exponential backoff on transient HTTP errors.

    Supabase's HTTP/2 pooler can reset streams mid-flight under load —
    seen as `httpx.RemoteProtocolError: <StreamReset ...>`. Without retry,
    one stream reset kills the whole sync (lost in-flight batch + lost
    progress on subsequent pages). Mirror the Instantly-side retry pattern.
    """
    for attempt in range(UPSERT_MAX_RETRIES):
        try:
            resp = (
                supabase.table(table)
                .upsert(batch, on_conflict="instantly_message_id",
                        ignore_duplicates=True)
                .execute()
            )
            return len(resp.data or [])
        except (httpx.HTTPError, ConnectionError, OSError) as e:
            if attempt == UPSERT_MAX_RETRIES - 1:
                print(f"  ! upsert {table} batch FAILED after "
                      f"{UPSERT_MAX_RETRIES} retries ({type(e).__name__}: {e}); "
                      f"dropping {len(batch)} rows and continuing",
                      file=sys.stderr)
                return 0
            sleep_s = min(60.0, (2 ** attempt) + random.random())
            print(f"  ! upsert {table} {type(e).__name__}; "
                  f"retry {attempt + 1}/{UPSERT_MAX_RETRIES} in {sleep_s:.1f}s",
                  file=sys.stderr)
            time.sleep(sleep_s)
    return 0


def upsert_rows(supabase, rows: list[dict], email_type: str) -> int:
    """Returns count of newly inserted rows (duplicates skipped via ignore_duplicates).

    Dispatches to the right table based on email_type:
      - received -> replies
      - sent     -> sent_messages
    Both tables have `instantly_message_id` as the unique conflict key.

    Each batch is retried independently on transient HTTP errors so one
    stream reset doesn't kill the whole sync.
    """
    if not rows:
        return 0
    table = TABLE_BY_TYPE[email_type]
    new_count = 0
    for batch in chunk(rows, INSERT_BATCH):
        new_count += _upsert_batch_with_retry(supabase, table, batch)
    return new_count


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser(prog="instantly_sync.py")
    parser.add_argument("--days", type=int, default=None,
                        help=f"Lookback window in days. If omitted, uses sync_state "
                             f"cursor (or {LOOKBACK_DAYS}-day default on first run).")
    parser.add_argument("--type", dest="email_type", choices=["received", "sent"],
                        default="received", help="Email type to fetch (default received)")
    parser.add_argument("--starting-after", default=None,
                        help="Resume pagination from this cursor")
    parser.add_argument("--sort", choices=["asc", "desc"], default="asc",
                        help="Pagination sort order. For wide backfills, use "
                             "'desc' — page 1 starts at 'now' instead of N days "
                             "ago, avoiding Instantly's first-page slowness on "
                             "wide windows.")
    args = parser.parse_args()

    load_dotenv()
    instantly_key = get_env("INSTANTLY_API_KEY")
    supabase_url = get_env("SUPABASE_URL")
    supabase_key = get_env("SUPABASE_KEY")

    supabase = create_client(supabase_url, supabase_key)
    if not verify_supabase(supabase, args.email_type):
        # Non-critical (sent) table transiently unreachable — skip this run's
        # sent pass cleanly (exit 0) so the daily cron chain continues.
        return

    session = make_session(instantly_key)
    limiter = RateLimiter(DEFAULT_MIN_INTERVAL_S)

    campaigns_map = fetch_all_campaigns(session, limiter)
    print(f"Fetched {len(campaigns_map)} campaigns")

    tags_map = fetch_campaign_tags(session, limiter)
    total_links = sum(len(v) for v in tags_map.values())
    print(f"Fetched {total_links} campaign-tag links across {len(tags_map)} campaigns")

    lead_labels = fetch_lead_labels(session, limiter)
    print(f"Fetched {len(lead_labels)} lead labels")

    # Lookback window precedence (FOLLOWUP_ANALYSIS_PLAN.md Phase 2 Edit C):
    #   1. --starting-after cursor -> paginator drives, no min_ts needed
    #   2. --days N (manual override)
    #   3. sync_state.last_synced_at for this email_type (incremental)
    #   4. LOOKBACK_DAYS default (first-ever run for this type)
    if args.starting_after:
        min_ts = None
        print(f"Resuming from cursor {args.starting_after} (--starting-after override)")
    elif args.days is not None:
        min_ts = (datetime.now(timezone.utc)
                  - timedelta(days=args.days)
                 ).strftime("%Y-%m-%dT%H:%M:%SZ")
        print(f"Pulling {args.email_type} emails since {min_ts} ({args.days}-day window, --days override)")
    else:
        try:
            cursor_resp = (supabase.table("sync_state")
                           .select("last_synced_at")
                           .eq("message_type", args.email_type)
                           .maybe_single().execute())
        except Exception as e:
            print(f"  ! sync_state lookup failed ({e}); falling back to {LOOKBACK_DAYS}-day default")
            cursor_resp = None
        last_ts = (cursor_resp.data or {}).get("last_synced_at") if cursor_resp else None
        if last_ts:
            min_ts = last_ts
            print(f"Pulling {args.email_type} emails since {min_ts} (sync_state cursor)")
        else:
            min_ts = (datetime.now(timezone.utc)
                      - timedelta(days=LOOKBACK_DAYS)
                     ).strftime("%Y-%m-%dT%H:%M:%SZ")
            print(f"Pulling {args.email_type} emails since {min_ts} ({LOOKBACK_DAYS}-day default — sync_state was null)")

    all_rows: list[dict] = []
    pending: list[dict] = []
    skipped_missing_id = 0
    skipped_missing_email = 0
    unknown_campaign = 0
    total_new = 0

    email_params = {
        "email_type": args.email_type,
        "sort_order": args.sort,
    }
    if min_ts:
        email_params["min_timestamp_created"] = min_ts
    last_cursor: str | None = args.starting_after

    DEBUG_DIR.mkdir(exist_ok=True)
    cursor_file = DEBUG_DIR / f"last_cursor_{args.email_type}.txt"

    ts_col = TS_COL_BY_TYPE[args.email_type]

    try:
        for page, cur, nxt, items in paginate(
            session, LIST_EMAILS_URL, email_params, limiter, "emails",
            start_cursor=args.starting_after,
        ):
            for email in items:
                row = parse_email(email, campaigns_map, tags_map, lead_labels, args.email_type)
                if not row["instantly_message_id"]:
                    skipped_missing_id += 1
                    continue
                if not row["lead_email"]:
                    skipped_missing_email += 1
                    continue
                if row["campaign_name"] == "UNKNOWN":
                    unknown_campaign += 1
                pending.append(row)
                all_rows.append(row)

            if nxt:
                last_cursor = nxt

            if page % FLUSH_EVERY_PAGES == 0 and pending:
                new_in_flush = upsert_rows(supabase, pending, args.email_type)
                total_new += new_in_flush
                print(f"  flushed page {page}: {len(pending)} rows ({new_in_flush} new); running total = {len(all_rows)}")
                pending = []
                if last_cursor:
                    cursor_file.write_text(last_cursor, encoding="utf-8")
    except SystemExit:
        if pending:
            total_new += upsert_rows(supabase, pending, args.email_type)
            pending = []
        if last_cursor:
            cursor_file.write_text(last_cursor, encoding="utf-8")
        print(f"\nSync interrupted. Last cursor saved to {cursor_file}")
        print(f"Resume with: --starting-after {last_cursor}")
        raise

    if pending:
        total_new += upsert_rows(supabase, pending, args.email_type)

    (DEBUG_DIR / "parsed_sample.json").write_text(
        json.dumps(all_rows[:3], indent=2, default=str), encoding="utf-8"
    )
    if cursor_file.exists():
        cursor_file.unlink()

    # Advance sync_state cursor for this email_type (Phase 2 Edit C).
    # Skip when --days or --starting-after was explicitly passed (those are
    # manual overrides; touching the cursor in those cases would surprise
    # subsequent incremental runs).
    if not args.starting_after and args.days is None and all_rows:
        max_seen = max((r.get(ts_col) for r in all_rows if r.get(ts_col)),
                       default=None)
        if max_seen:
            try:
                supabase.table("sync_state").update(
                    {"last_synced_at": max_seen}
                ).eq("message_type", args.email_type).execute()
                print(f"Cursor advanced: sync_state.{args.email_type}.last_synced_at = {max_seen}")
            except Exception as e:
                print(f"  ! failed to advance sync_state cursor: {e}", file=sys.stderr)

    total_fetched = len(all_rows)
    dup_count = total_fetched - total_new

    print(f"Parsed {total_fetched} valid replies "
          f"(skipped {skipped_missing_id} missing id, {skipped_missing_email} missing email)")

    if unknown_campaign:
        print(f"WARNING: {unknown_campaign} replies had campaign_id not in campaigns_map (stored as 'UNKNOWN')")

    print()
    print(f"Synced {total_fetched} {args.email_type} emails ({total_new} new, {dup_count} duplicates, {unknown_campaign} unknown-campaign)")

    by_month: Counter = Counter()
    for r in all_rows:
        ts = r.get(ts_col) or ""
        if len(ts) >= 7:
            by_month[ts[:7]] += 1
    if by_month:
        print()
        print(f"{args.email_type.capitalize()} count per month (fetched this run):")
        for ym in sorted(by_month):
            print(f"  {ym}  {by_month[ym]}")

    # Outbound-only summary: send_kind split for the rows we just processed.
    if args.email_type == "sent" and all_rows:
        send_kind_counts: Counter = Counter()
        for r in all_rows:
            step = r.get("step")
            ue = r.get("ue_type")
            if step is None:
                send_kind_counts["unibox_manual"] += 1
            elif ue == 1:
                send_kind_counts["campaign_auto"] += 1
            else:
                send_kind_counts["unknown"] += 1
        print()
        print("Send-kind split for this run:")
        for k, v in send_kind_counts.most_common():
            print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
