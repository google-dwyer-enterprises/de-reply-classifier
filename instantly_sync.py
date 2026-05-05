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
    for attempt in range(MAX_RETRIES):
        limiter.wait()
        resp = session.get(url, params=params, timeout=30)
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


def verify_supabase(client) -> None:
    try:
        resp = client.table("replies").select("*", count="exact", head=True).execute()
    except Exception as e:
        sys.exit(f"FATAL: cannot access Supabase 'replies' table: {e}")
    count = resp.count if resp.count is not None else "?"
    print(f"Supabase OK — replies table reachable (current rowcount={count})")


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
) -> dict:
    raw_from = email.get("from_address_email") or ""
    lead_email = raw_from.strip().lower()

    campaign_id = email.get("campaign_id")
    campaign_name = campaigns_map.get(campaign_id, "UNKNOWN") if campaign_id else "UNKNOWN"

    body_obj = email.get("body") or {}
    body_text = (body_obj.get("text") or "").strip()
    if not body_text:
        body_text = html_to_text(body_obj.get("html") or "")

    return {
        "lead_email": lead_email,
        "campaign_id": campaign_id,
        "campaign_name": campaign_name,
        "client": extract_client(campaign_name),
        "reply_timestamp": email.get("timestamp_email"),
        "subject": email.get("subject"),
        "body": body_text,
        "instantly_message_id": email.get("id"),
        "thread_id": email.get("thread_id"),
        "tags": tags_map.get(campaign_id, []) if campaign_id else [],
        "lead_status_code": email.get("i_status"),
        "lead_status": lead_labels.get(email.get("i_status")) if email.get("i_status") is not None else None,
    }


# --------------------------------------------------------------------------- #
# Upsert
# --------------------------------------------------------------------------- #

def chunk(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def upsert_replies(supabase, rows: list[dict]) -> int:
    """Returns count of newly inserted rows (duplicates skipped via ignore_duplicates)."""
    new_count = 0
    for batch in chunk(rows, INSERT_BATCH):
        resp = (
            supabase.table("replies")
            .upsert(batch, on_conflict="instantly_message_id", ignore_duplicates=True)
            .execute()
        )
        new_count += len(resp.data or [])
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
    parser.add_argument("--days", type=int, default=LOOKBACK_DAYS,
                        help=f"Lookback window in days (default {LOOKBACK_DAYS})")
    parser.add_argument("--type", dest="email_type", choices=["received", "sent"],
                        default="received", help="Email type to fetch (default received)")
    parser.add_argument("--starting-after", default=None,
                        help="Resume pagination from this cursor")
    args = parser.parse_args()

    load_dotenv()
    instantly_key = get_env("INSTANTLY_API_KEY")
    supabase_url = get_env("SUPABASE_URL")
    supabase_key = get_env("SUPABASE_KEY")

    supabase = create_client(supabase_url, supabase_key)
    verify_supabase(supabase)

    session = make_session(instantly_key)
    limiter = RateLimiter(DEFAULT_MIN_INTERVAL_S)

    campaigns_map = fetch_all_campaigns(session, limiter)
    print(f"Fetched {len(campaigns_map)} campaigns")

    tags_map = fetch_campaign_tags(session, limiter)
    total_links = sum(len(v) for v in tags_map.values())
    print(f"Fetched {total_links} campaign-tag links across {len(tags_map)} campaigns")

    lead_labels = fetch_lead_labels(session, limiter)
    print(f"Fetched {len(lead_labels)} lead labels")

    min_ts = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"Pulling {args.email_type} emails since {min_ts} ({args.days}-day window)")

    all_rows: list[dict] = []
    pending: list[dict] = []
    skipped_missing_id = 0
    skipped_missing_email = 0
    unknown_campaign = 0
    total_new = 0

    email_params = {
        "email_type": args.email_type,
        "min_timestamp_created": min_ts,
        "sort_order": "asc",
    }
    last_cursor: str | None = args.starting_after

    DEBUG_DIR.mkdir(exist_ok=True)
    cursor_file = DEBUG_DIR / f"last_cursor_{args.email_type}.txt"

    try:
        for page, cur, nxt, items in paginate(
            session, LIST_EMAILS_URL, email_params, limiter, "emails",
            start_cursor=args.starting_after,
        ):
            for email in items:
                row = parse_email(email, campaigns_map, tags_map, lead_labels)
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
                new_in_flush = upsert_replies(supabase, pending)
                total_new += new_in_flush
                print(f"  flushed page {page}: {len(pending)} rows ({new_in_flush} new); running total = {len(all_rows)}")
                pending = []
                if last_cursor:
                    cursor_file.write_text(last_cursor, encoding="utf-8")
    except SystemExit:
        if pending:
            total_new += upsert_replies(supabase, pending)
            pending = []
        if last_cursor:
            cursor_file.write_text(last_cursor, encoding="utf-8")
        print(f"\nSync interrupted. Last cursor saved to {cursor_file}")
        print(f"Resume with: --starting-after {last_cursor}")
        raise

    if pending:
        total_new += upsert_replies(supabase, pending)

    (DEBUG_DIR / "parsed_sample.json").write_text(
        json.dumps(all_rows[:3], indent=2, default=str), encoding="utf-8"
    )
    if cursor_file.exists():
        cursor_file.unlink()

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
        ts = r.get("reply_timestamp") or ""
        if len(ts) >= 7:
            by_month[ts[:7]] += 1
    if by_month:
        print()
        print(f"{args.email_type.capitalize()} count per month (fetched this run):")
        for ym in sorted(by_month):
            print(f"  {ym}  {by_month[ym]}")


if __name__ == "__main__":
    main()
