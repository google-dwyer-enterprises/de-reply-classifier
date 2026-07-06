"""Phase 3: Classifier.

Pulls unclassified replies from Supabase, cleans their bodies, batches them
to Claude Haiku 4.5, and writes results to `classifications`. Failures go to
`classification_errors`.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client

from config import (
    BATCH_SIZE,
    BODY_CHAR_LIMIT,
    LABEL_DEFINITIONS,
    LABELS,
    MAX_TOKENS,
    MODEL,
    PROMPT_VERSION,
)

PROMPT_PATH = Path(__file__).parent / "prompts" / "classifier.txt"
MAX_RETRIES = 5

LABEL_SET = set(LABELS)


# --------------------------------------------------------------------------- #
# Body cleaning
# --------------------------------------------------------------------------- #

_QUOTE_CUT_PATTERNS = [
    re.compile(r"^-{3,}\s*original message\s*-{3,}", re.I | re.M),
    re.compile(r"^On .+ wrote:\s*$", re.M),
    re.compile(
        r"^From:\s+.+$\s*(Reply-To:.+$\s*)?Date:.+$\s*To:.+$\s*Subject:.+$",
        re.M,
    ),
    re.compile(r"^From:\s+.+$\s*Sent:.+$\s*To:.+$\s*Subject:.+$", re.M),
]

_QUOTED_LINE_PATTERN = re.compile(r"^>+\s*.*$")

_SIG_CUT_PATTERNS = [
    re.compile(r"^--\s*$", re.M),
    re.compile(r"^Best,?\s*$", re.I | re.M),
    re.compile(r"^Cheers,?\s*$", re.I | re.M),
    re.compile(r"^Regards,?\s*$", re.I | re.M),
    re.compile(r"^Thanks,?\s*$", re.I | re.M),
    re.compile(r"^Sincerely,?\s*$", re.I | re.M),
    re.compile(r"^Sent from my \w+", re.I | re.M),
    re.compile(r"^Get Outlook for \w+", re.I | re.M),
]


def clean_body(body: str) -> str:
    if not body:
        return ""

    cut = len(body)
    for pat in _QUOTE_CUT_PATTERNS:
        m = pat.search(body)
        if m:
            cut = min(cut, m.start())
    body = body[:cut]

    lines = [ln for ln in body.splitlines() if not _QUOTED_LINE_PATTERN.match(ln.lstrip())]
    body = "\n".join(lines)

    sig_cut = len(body)
    for pat in _SIG_CUT_PATTERNS:
        m = pat.search(body)
        if m:
            sig_cut = min(sig_cut, m.start())
    body = body[:sig_cut]

    body = re.sub(r"[ \t]+", " ", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()

    if len(body) > BODY_CHAR_LIMIT:
        body = body[:BODY_CHAR_LIMIT]

    return body


# --------------------------------------------------------------------------- #
# Promo pre-filter
# --------------------------------------------------------------------------- #

_PROMO_SENDER_PREFIXES = ("newsletter@", "marketing@")
_WEAK_PROMO_KEYWORDS = ("unsubscribe", "shop now", "limited edition", "new arrivals")
_STRONG_PROMO_PHRASES = (
    "view in browser",
    "view this email",
    "manage your preferences",
    "update subscription preferences",
)
_MARKDOWN_LINK_PATTERN = re.compile(r"\[[^\]]+\]\(https?://")
_PROMO_WEAK_THRESHOLD = 3


def promo_score(subject: str, body: str, lead_email: str) -> int:
    body_s = body or ""
    body_lc = body_s.lower()
    email_lc = (lead_email or "").lower()

    if body_s.count("=C2=A0") >= 3:
        return 99
    if len(_MARKDOWN_LINK_PATTERN.findall(body_s)) >= 5:
        return 99
    if any(p in body_lc for p in _STRONG_PROMO_PHRASES):
        return 99

    score = 0
    for kw in _WEAK_PROMO_KEYWORDS:
        if kw in body_lc:
            score += 1
    if any(email_lc.startswith(p) for p in _PROMO_SENDER_PREFIXES):
        score += 1
    return score


def is_likely_promo(subject: str, body: str, lead_email: str) -> bool:
    return promo_score(subject, body, lead_email) >= _PROMO_WEAK_THRESHOLD


def _is_likely_promo_old(subject: str, body: str, lead_email: str) -> bool:
    """Previous (pre-tightening) implementation. Kept only for filter-diff reporting."""
    score = 0
    body_lc = (body or "").lower()
    if any(kw in body_lc for kw in ("unsubscribe", "view in browser", "shop now", "limited edition")):
        score += 1
    if "=C2=A0" in (body or ""):
        score += 1
    email_lc = (lead_email or "").lower()
    if any(email_lc.startswith(p) for p in ("help@", "info@", "newsletter@", "noreply@", "marketing@")):
        score += 1
    return score >= 2


_THREAD_MARKERS = (
    re.compile(r"On .+ wrote:", re.I),
    re.compile(r"^From:\s+.+$", re.I | re.M),
    re.compile(r"-{3,}\s*Original Message\s*-{3,}", re.I),
)


def looks_non_english(body: str) -> bool:
    if not body:
        return False
    non_ascii = sum(1 for c in body if ord(c) > 127)
    return non_ascii / max(len(body), 1) > 0.15


def looks_long_thread(body: str) -> bool:
    if not body:
        return False
    count = sum(len(p.findall(body)) for p in _THREAD_MARKERS)
    return count >= 2


_OOF_HINTS = (
    "out of office",
    "out-of-office",
    "automatic reply",
    "auto reply",
    "auto-reply",
    "on vacation",
    "on leave",
    "away from the office",
    "away from my desk",
    "annual leave",
)


def looks_like_oof(subject: str, body: str) -> bool:
    text = ((subject or "") + " " + (body or "")).lower()
    return any(h in text for h in _OOF_HINTS)


def select_variety(replies: list[dict], target: int = 10) -> list[dict]:
    buckets: dict[str, list[dict]] = {
        "promo": [], "oof": [], "non_english": [], "long_thread": [], "short": [], "sigbloat": [],
    }
    for r in replies:
        body = r.get("body") or ""
        subj = r.get("subject") or ""
        lead = r.get("lead_email") or ""
        if promo_score(subj, body, lead) >= 2:
            buckets["promo"].append(r)
            continue
        if looks_like_oof(subj, body):
            buckets["oof"].append(r)
            continue
        if looks_non_english(body):
            buckets["non_english"].append(r)
            continue
        if looks_long_thread(body):
            buckets["long_thread"].append(r)
            continue
        cleaned = clean_body(body)
        if cleaned and len(cleaned) < 20:
            buckets["short"].append(r)
            continue
        if len(body) > 500 and len(cleaned) > 300:
            buckets["sigbloat"].append(r)

    if target <= 10:
        quotas = [("promo", 2), ("short", 2), ("sigbloat", 2), ("oof", 2)]
    else:
        quotas = [
            ("sigbloat", 10), ("short", 10), ("oof", 3), ("promo", 3),
            ("non_english", 2), ("long_thread", 2),
        ]

    rng = random.Random(42)
    picked_ids: set = set()
    picked: list[dict] = []
    for name, n in quotas:
        pool = buckets[name][:]
        rng.shuffle(pool)
        for r in pool[:n]:
            if r["id"] not in picked_ids:
                picked.append(r)
                picked_ids.add(r["id"])

    leftovers = [r for r in replies if r["id"] not in picked_ids]
    rng.shuffle(leftovers)
    for r in leftovers:
        if len(picked) >= target:
            break
        picked.append(r)

    print(
        "Variety buckets — "
        + ", ".join(f"{k}: {len(v)}" for k, v in buckets.items())
    )
    return picked[:target]


# --------------------------------------------------------------------------- #
# Prompt assembly
# --------------------------------------------------------------------------- #

def build_system_prompt() -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    label_block = "\n".join(f"- {name}: {defn}" for name, defn in LABEL_DEFINITIONS.items())
    return template.replace("{label_block}", label_block)


def format_batch_user_message(batch: list[dict]) -> str:
    lines = [f"Classify these {len(batch)} replies:", ""]
    for i, reply in enumerate(batch, 1):
        cleaned = clean_body(reply.get("body") or "")
        subject = (reply.get("subject") or "").strip()
        email = (reply.get("lead_email") or "").strip()
        cleaned_one_line = cleaned.replace("\n", " ").strip()
        lines.append(f"[{i}] FROM: {email} | SUBJECT: {subject} | BODY: {cleaned_one_line}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Supabase helpers
# --------------------------------------------------------------------------- #

def _paginate_all(query_builder, page_size: int = 1000) -> list[dict]:
    out: list[dict] = []
    start = 0
    while True:
        resp = query_builder.range(start, start + page_size - 1).execute()
        batch = resp.data or []
        out.extend(batch)
        if len(batch) < page_size:
            return out
        start += page_size


def fetch_unclassified(supabase, limit: int | None) -> list[dict]:
    classified_rows = _paginate_all(supabase.table("classifications").select("reply_id"))
    classified_ids = {row["reply_id"] for row in classified_rows if row.get("reply_id")}

    all_replies = _paginate_all(
        supabase.table("replies").select("*").order("reply_timestamp")
    )
    unclassified = [r for r in all_replies if r["id"] not in classified_ids]
    if limit is not None:
        unclassified = unclassified[:limit]
    return unclassified


def write_classifications(supabase, rows: list[dict]) -> None:
    if not rows:
        return
    supabase.table("classifications").insert(rows).execute()


def classify_promos(supabase, promos: list[dict]) -> None:
    rows = [
        {
            "reply_id": r["id"],
            "lead_email": r["lead_email"],
            "label": "other",
            "confidence": 1.00,
            "model": "rule-based",
            "prompt_version": PROMPT_VERSION,
            "alternate_contact": None,
            "reason": "auto-detected brand promo",
            "raw_response": {
                "label": "other",
                "confidence": 1.00,
                "reason": "auto-detected brand promo",
                "source": "rule-based",
            },
        }
        for r in promos
    ]
    write_classifications(supabase, rows)


def write_error(supabase, row: dict) -> None:
    try:
        supabase.table("classification_errors").insert(row).execute()
    except Exception as e:
        print(f"  WARN: could not write to classification_errors: {e}")
        print(f"  error_payload: {json.dumps(row, default=str)[:500]}")


# --------------------------------------------------------------------------- #
# Anthropic call + parsing
# --------------------------------------------------------------------------- #

def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def call_haiku(anthropic_client, system_prompt: str, user_message: str, model: str = MODEL) -> str:
    for attempt in range(MAX_RETRIES):
        try:
            resp = anthropic_client.messages.create(
                model=model,
                max_tokens=MAX_TOKENS,
                system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user_message}],
            )
            return resp.content[0].text
        except Exception as e:
            status = getattr(e, "status_code", None)
            if status == 429 or (status is not None and 500 <= status < 600):
                sleep_s = (2 ** attempt) + random.random()
                print(f"  API {status}; backoff {sleep_s:.1f}s (attempt {attempt + 1}/{MAX_RETRIES})")
                time.sleep(sleep_s)
                continue
            # Out-of-credit / billing rejection (e.g. "credit balance is too low"):
            # email a renew reminder before bubbling up, so a dead key doesn't just
            # silently break the cron. Best-effort, throttled, never masks the error.
            try:
                import credit_alerts
                credit_alerts.maybe_alert("Anthropic", str(e))
            except Exception:
                pass
            raise
    raise RuntimeError(f"exhausted {MAX_RETRIES} retries on Anthropic API")


def parse_response(text: str) -> list[dict]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    return json.loads(stripped)


# --------------------------------------------------------------------------- #
# Classification loop
# --------------------------------------------------------------------------- #

def classify_batch(anthropic_client, supabase, system_prompt: str, batch: list[dict], model: str = MODEL) -> None:
    user_message = format_batch_user_message(batch)
    raw = None
    try:
        raw = call_haiku(anthropic_client, system_prompt, user_message, model=model)
    except Exception as e:
        write_error(
            supabase,
            {
                "reply_id": None,
                "error_type": "api_error",
                "error_message": str(e)[:2000],
                "raw_response": None,
                "batch_ids": [r["id"] for r in batch],
                "prompt_version": PROMPT_VERSION,
            },
        )
        print(f"  batch failed (api_error): {e}")
        return

    try:
        parsed = parse_response(raw)
    except Exception as e:
        write_error(
            supabase,
            {
                "reply_id": None,
                "error_type": "json_parse",
                "error_message": str(e)[:2000],
                "raw_response": raw[:4000] if raw else None,
                "batch_ids": [r["id"] for r in batch],
                "prompt_version": PROMPT_VERSION,
            },
        )
        print(f"  batch failed (json_parse): {e}")
        return

    by_index = {item["id"]: item for item in parsed if isinstance(item, dict) and "id" in item}
    rows_to_insert: list[dict] = []

    for idx, reply in enumerate(batch, 1):
        item = by_index.get(idx)
        if item is None:
            write_error(
                supabase,
                {
                    "reply_id": reply["id"],
                    "error_type": "missing_row",
                    "error_message": f"no item for idx {idx} in response",
                    "raw_response": raw[:4000] if raw else None,
                    "batch_ids": None,
                    "prompt_version": PROMPT_VERSION,
                },
            )
            continue

        label = item.get("label")
        if label not in LABEL_SET:
            write_error(
                supabase,
                {
                    "reply_id": reply["id"],
                    "error_type": "invalid_label",
                    "error_message": f"label={label!r}",
                    "raw_response": json.dumps(item)[:4000],
                    "batch_ids": None,
                    "prompt_version": PROMPT_VERSION,
                },
            )
            continue

        rows_to_insert.append(
            {
                "reply_id": reply["id"],
                "lead_email": reply["lead_email"],
                "label": label,
                "confidence": item.get("confidence"),
                "model": model,
                "prompt_version": PROMPT_VERSION,
                "alternate_contact": item.get("alternate_contact"),
                "reason": (str(item.get("reason"))[:500] if item.get("reason") else None),
                "raw_response": item,
            }
        )

    write_classifications(supabase, rows_to_insert)
    print(f"  wrote {len(rows_to_insert)} classifications")


# --------------------------------------------------------------------------- #
# Dry run
# --------------------------------------------------------------------------- #

def dry_run(supabase, system_prompt: str) -> None:
    sample = fetch_unclassified(supabase, limit=3)
    if not sample:
        print("No unclassified replies found.")
        return

    print("=" * 72)
    print("SYSTEM PROMPT")
    print("=" * 72)
    print(system_prompt)
    print()

    print("=" * 72)
    print("PER-REPLY CLEANING + PROMO SCORE")
    print("=" * 72)
    to_haiku: list[dict] = []
    for i, r in enumerate(sample, 1):
        raw = (r.get("body") or "").strip()
        subject = r.get("subject") or ""
        lead = r.get("lead_email") or ""
        score = promo_score(subject, raw, lead)
        is_promo = score >= 2
        cleaned = clean_body(raw)
        print(f"\n[{i}] reply_id={r['id']} lead_email={lead}")
        print(f"    subject: {subject}")
        print(f"    promo_score: {score}  would_skip_as_promo: {is_promo}")
        print(f"--- ORIGINAL ({len(raw)} chars) ---")
        print(raw[:1500] + ("..." if len(raw) > 1500 else ""))
        print(f"--- CLEANED ({len(cleaned)} chars) ---")
        print(cleaned)
        if is_promo:
            print("    >>> WOULD WRITE DIRECTLY: label=other, confidence=1.00, model=rule-based")
        else:
            to_haiku.append(r)

    print()
    print("=" * 72)
    print(f"USER MESSAGE (what gets sent to Haiku — {len(to_haiku)} of {len(sample)} replies)")
    print("=" * 72)
    if to_haiku:
        print(format_batch_user_message(to_haiku))
    else:
        print("(no replies would be sent — all filtered as promo)")
    print()
    print("=" * 72)
    print("No API call made (dry run).")


# --------------------------------------------------------------------------- #
# Cost estimator
# --------------------------------------------------------------------------- #

# Per 1M tokens. cache_write = 1.25× base input, cache_read = 0.1× base input.
PRICING_PER_M = {
    "claude-haiku-4-5":   {"input": 1.0, "cache_write": 1.25, "cache_read": 0.10, "output": 5.0},
    "claude-sonnet-4-6":  {"input": 3.0, "cache_write": 3.75, "cache_read": 0.30, "output": 15.0},
}
CHARS_PER_TOKEN = 4   # rough English estimate
OUTPUT_TOKENS_PER_REPLY = 40   # label + confidence + short reason JSON


def cost_estimate(supabase, system_prompt: str, reclassify: bool = False) -> None:
    if reclassify:
        replies = _paginate_all(supabase.table("replies").select("*"))
        scope = "ALL replies (reclassify)"
    else:
        replies = fetch_unclassified(supabase, limit=None)
        scope = "UNCLASSIFIED replies only"
    if not replies:
        print("No replies. Nothing to estimate.")
        return
    print(f"Scope: {scope}")

    promos = [
        r for r in replies
        if is_likely_promo(r.get("subject") or "", r.get("body") or "", r.get("lead_email") or "")
    ]
    promo_ids = {r["id"] for r in promos}
    to_api = [r for r in replies if r["id"] not in promo_ids]

    print("=" * 60)
    print("CLASSIFY COST ESTIMATE")
    print("=" * 60)
    print(f"Unclassified replies:    {len(replies):,}")
    print(f"  Promo (rule-based):    {len(promos):,}  (free, no API)")
    print(f"  → going to API:        {len(to_api):,}")

    if not to_api:
        print("Nothing to send to API.")
        return

    sys_tokens = max(1, len(system_prompt) // CHARS_PER_TOKEN)

    sample = to_api[: BATCH_SIZE * 5]
    sample_batches = list(chunks(sample, BATCH_SIZE))
    user_token_samples = [
        len(format_batch_user_message(b)) // CHARS_PER_TOKEN for b in sample_batches
    ]
    avg_user_tokens = (sum(user_token_samples) / len(user_token_samples)) if user_token_samples else 1500

    n_batches = (len(to_api) + BATCH_SIZE - 1) // BATCH_SIZE
    output_tokens_per_batch = OUTPUT_TOKENS_PER_REPLY * BATCH_SIZE

    print(f"Batches ({BATCH_SIZE}/batch):       {n_batches:,}")
    print(f"System prompt tokens:    ~{sys_tokens:,}")
    print(f"Avg user tokens/batch:   ~{avg_user_tokens:,.0f}")
    print(f"Avg output tokens/batch: ~{output_tokens_per_batch:,}")
    print()

    for model_name, p in PRICING_PER_M.items():
        # Assume 1 cache-write batch then (n-1) cache-read batches (cache TTL 5 min,
        # batches run back-to-back so the assumption mostly holds).
        cache_write = (sys_tokens / 1_000_000) * p["cache_write"]
        cache_read  = (sys_tokens / 1_000_000) * p["cache_read"] * max(0, n_batches - 1)
        user_cost   = (avg_user_tokens * n_batches / 1_000_000) * p["input"]
        out_cost    = (output_tokens_per_batch * n_batches / 1_000_000) * p["output"]
        total = cache_write + cache_read + user_cost + out_cost

        marker = "  (current)" if model_name == MODEL else ""
        print(f"=== {model_name}{marker} ===")
        print(f"  Cache write (batch 1):     ${cache_write:.4f}")
        print(f"  Cache read (batches 2..N): ${cache_read:.4f}")
        print(f"  User messages:             ${user_cost:.4f}")
        print(f"  Output:                    ${out_cost:.4f}")
        print(f"  TOTAL:                     ${total:.2f}")
        print()

    print("Note: estimate uses ~4 chars/token. Real cost typically within ±15%.")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def get_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        sys.exit(f"FATAL: {name} is not set in .env")
    return val


def print_promo_filter_diff(supabase) -> None:
    """Report replies where the old promo filter flagged, but the tightened filter would not."""
    resp = supabase.table("classifications").select("reply_id").eq("model", "rule-based").execute()
    ids = [r["reply_id"] for r in (resp.data or []) if r.get("reply_id")]
    if not ids:
        print("No rule-based classifications to diff.")
        return
    r = supabase.table("replies").select("id, lead_email, subject, body").in_("id", ids).execute()
    releases: list[dict] = []
    for row in r.data or []:
        subj = row.get("subject") or ""
        body = row.get("body") or ""
        lead = row.get("lead_email") or ""
        if _is_likely_promo_old(subj, body, lead) and not is_likely_promo(subj, body, lead):
            releases.append(row)

    print()
    print("=" * 72)
    print("PROMO FILTER DIFF — replies the tightened filter would release")
    print("=" * 72)
    if not releases:
        print("None. All existing rule-based rows would still be flagged as promo.")
        return
    for row in releases:
        prev = _safe_preview(row.get("body") or "", 80)
        print(f"  {row['id']:>5} | {row['lead_email']:30.30} | {prev}")
    print(f"\n{len(releases)} of {len(ids)} would be released (consider re-classifying under Haiku)")


def print_total_count(supabase) -> None:
    resp = supabase.table("classifications").select("*", count="exact", head=True).execute()
    print(f"Total classifications in DB: {resp.count}")


def print_version_diff(supabase, replies: list[dict], v_left: str, v_right: str) -> None:
    ids = [r["id"] for r in replies]
    resp = (
        supabase.table("classifications")
        .select("reply_id, prompt_version, label, confidence")
        .in_("reply_id", ids)
        .in_("prompt_version", [v_left, v_right])
        .execute()
    )
    by_reply: dict[int, dict[str, dict]] = {}
    for row in resp.data or []:
        by_reply.setdefault(row["reply_id"], {})[row["prompt_version"]] = row

    print()
    print("=" * 72)
    print(f"DIFF: {v_left} vs {v_right}")
    print("=" * 72)
    print(f"{'id':>5} | {v_left + '_label':18} {'conf':>5} | {v_right + '_label':18} {'conf':>5}")
    print("-" * 72)
    diffs = 0
    missing = 0
    for r in replies:
        rid = r["id"]
        rows = by_reply.get(rid, {})
        a = rows.get(v_left)
        b = rows.get(v_right)
        if not (a and b):
            missing += 1
            continue
        if a["label"] != b["label"]:
            diffs += 1
            print(
                f"{rid:>5} | {a['label']:18} {float(a['confidence'] or 0):>5.2f} | "
                f"{b['label']:18} {float(b['confidence'] or 0):>5.2f}"
            )
    print("-" * 72)
    print(f"Differing labels: {diffs} of {len(replies) - missing} paired rows ({missing} missing a pair)")


def print_summary(supabase, replies: list[dict], prompt_version: str | None = None) -> None:
    ids = [r["id"] for r in replies]
    q = (
        supabase.table("classifications")
        .select("label, confidence, prompt_version")
        .in_("reply_id", ids)
    )
    if prompt_version:
        q = q.eq("prompt_version", prompt_version)
    resp = q.execute()
    rows = resp.data or []

    from collections import defaultdict
    counts: dict[str, int] = defaultdict(int)
    conf_sum: dict[str, float] = defaultdict(float)
    conf_n: dict[str, int] = defaultdict(int)
    low_conf = 0

    for row in rows:
        label = row["label"]
        counts[label] += 1
        c = row.get("confidence")
        if c is not None:
            conf_sum[label] += float(c)
            conf_n[label] += 1
            if float(c) < 0.80:
                low_conf += 1

    print()
    print("=" * 60)
    version_tag = f" (prompt_version={prompt_version})" if prompt_version else ""
    print(f"SUMMARY — {len(rows)} classifications{version_tag}")
    print("=" * 60)
    print(f"{'label':20} {'count':>6} {'avg_conf':>10}")
    print("-" * 40)
    for label in sorted(counts, key=lambda k: (-counts[k], k)):
        avg = (conf_sum[label] / conf_n[label]) if conf_n[label] else 0.0
        print(f"{label:20} {counts[label]:>6} {avg:>10.2f}")
    print("-" * 40)
    print(f"Classifications with confidence < 0.80: {low_conf} (flagged for review)")


def print_results_table(supabase, replies: list[dict], prompt_version: str | None = None) -> None:
    ids = [r["id"] for r in replies]
    q = (
        supabase.table("classifications")
        .select("reply_id, lead_email, label, confidence, alternate_contact, raw_response, prompt_version")
        .in_("reply_id", ids)
    )
    if prompt_version:
        q = q.eq("prompt_version", prompt_version)
    resp = q.execute()
    by_reply = {row["reply_id"]: row for row in (resp.data or [])}
    body_by_reply = {r["id"]: (r.get("body") or "") for r in replies}

    header = f"{'id':>5} | {'lead_email':30.30} | {'label':16.16} | {'conf':>4} | {'reason':40.40} | {'alt_contact':25.25} | body_preview"
    print()
    print(header)
    print("-" * len(header))
    for r in replies:
        c = by_reply.get(r["id"])
        if not c:
            print(f"{r['id']:>5} | {r['lead_email']:30.30} | {'(no row)':16.16} | {'':>4} | {'':40.40} | {'':25.25} | ")
            continue
        raw = c.get("raw_response") or {}
        reason = (raw.get("reason") or "")[:40] if isinstance(raw, dict) else ""
        alt = (c.get("alternate_contact") or "")[:25]
        conf = c.get("confidence")
        conf_s = f"{conf:.2f}" if conf is not None else ""
        body_prev = _safe_preview(body_by_reply.get(r["id"], ""), 80)
        print(
            f"{c['reply_id']:>5} | {c['lead_email']:30.30} | {c['label']:16.16} | "
            f"{conf_s:>4} | {reason:40.40} | {alt:25.25} | {body_prev}"
        )


def _safe_preview(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", s or "")
    s = "".join(c if 0x20 <= ord(c) < 0x7F else "?" for c in s)
    return s[:n]


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="cap number of replies to classify")
    parser.add_argument("--dry-run", action="store_true", help="print the prompt for 3 replies; no API call")
    parser.add_argument("--cost-estimate", action="store_true",
                        help="estimate cost (Haiku vs Sonnet) for all unclassified replies; no API/DB writes")
    parser.add_argument("--model", type=str, default=MODEL,
                        help=f"override model (default {MODEL}). Try claude-sonnet-4-6 for higher accuracy.")
    parser.add_argument("--variety", action="store_true", help="pick a variety-balanced sample instead of chronological")
    parser.add_argument("--reclassify", action="store_true", help="include already-classified replies (keeps prior rows for diffing)")
    parser.add_argument("--diff-against", type=str, default=None, help="after run, diff current prompt_version against this one (e.g. v1)")
    args = parser.parse_args()

    load_dotenv()
    supabase_url = get_env("SUPABASE_URL")
    supabase_key = get_env("SUPABASE_KEY")
    supabase = create_client(supabase_url, supabase_key)

    system_prompt = build_system_prompt()

    if args.dry_run:
        dry_run(supabase, system_prompt)
        return

    if args.cost_estimate:
        cost_estimate(supabase, system_prompt, reclassify=args.reclassify)
        return

    from anthropic import Anthropic
    anthropic_key = get_env("ANTHROPIC_API_KEY")
    anthropic_client = Anthropic(api_key=anthropic_key)

    if args.reclassify and args.variety:
        # _paginate_all, not raw .execute(): PostgREST caps a single request at 1000 rows,
        # which silently truncated full reclassifies to the first 1000 replies.
        all_replies = _paginate_all(supabase.table("replies").select("*").order("reply_timestamp"))
        replies = select_variety(all_replies, target=args.limit or 10)
    elif args.reclassify:
        replies = _paginate_all(supabase.table("replies").select("*").order("reply_timestamp"))
        if args.limit:
            replies = replies[: args.limit]
    elif args.variety:
        all_unclassified = fetch_unclassified(supabase, limit=None)
        replies = select_variety(all_unclassified, target=args.limit or 10)
    else:
        replies = fetch_unclassified(supabase, limit=args.limit)

    promos = [r for r in replies if is_likely_promo(r.get("subject") or "", r.get("body") or "", r.get("lead_email") or "")]
    promo_ids = {r["id"] for r in promos}
    to_haiku = [r for r in replies if r["id"] not in promo_ids]
    print(f"Selected: {len(replies)} | promo-filtered: {len(promos)} | to Haiku: {len(to_haiku)}")

    if promos:
        classify_promos(supabase, promos)
        print(f"Wrote {len(promos)} rule-based promo classifications")

    if args.model != MODEL:
        print(f"Model override: using {args.model} (default is {MODEL})")
    for batch_num, batch in enumerate(chunks(to_haiku, BATCH_SIZE), 1):
        print(f"Batch {batch_num} ({len(batch)} replies)")
        classify_batch(anthropic_client, supabase, system_prompt, batch, model=args.model)

    print(f"Done. Classified {len(replies)} replies (prompt_version={PROMPT_VERSION}, model={args.model}).")
    is_full_run = not (args.limit or args.dry_run or args.variety or args.reclassify)
    if is_full_run:
        print_total_count(supabase)
        print_summary(supabase, replies, prompt_version=PROMPT_VERSION)
        print_promo_filter_diff(supabase)
    else:
        print_results_table(supabase, replies, prompt_version=PROMPT_VERSION)
        print_summary(supabase, replies, prompt_version=PROMPT_VERSION)
        if args.diff_against:
            print_version_diff(supabase, replies, args.diff_against, PROMPT_VERSION)


if __name__ == "__main__":
    main()
