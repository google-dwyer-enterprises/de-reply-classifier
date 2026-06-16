"""Phase 2 — LLM tagging of manual follow-up messages (hook/tone/CTA/personalization).

For every manual follow-up that already has cleanly-extracted new text
(`followup_message_features.boundary_detected`), ask the model to assign four
closed-enum style tags over the rep's NEW text (the quoted thread is already
stripped by followup_features.extract_new_text — V1). Writes the tags + model
provenance into the nullable V2 columns. These then feed followup_patterns_mv
exactly like the deterministic features, so "which styles get positive replies"
gets hook/tone/CTA dimensions on top of length/has-question/etc.

DESCRIPTIVE only — same caveats as the V1 analysis (associations, not causation).

This is a MANUAL, gated, paid pass (like llm-resolve-smartscout): it prints the
model + estimated cost and asks before spending. Idempotent + versioned on
`llm_prompt_version` — re-running only tags rows that are untagged or stale.

Usage:
    python followup_llm_features.py --dry-run        # print prompt + first batch, no API call
    python followup_llm_features.py --limit 25       # tag a small sample (validate quality first)
    python followup_llm_features.py                  # tag all untagged/stale (asks to confirm)
    python followup_llm_features.py --retag --yes    # force re-tag everything, no prompt
"""
from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv
from psycopg2.extras import execute_values

from classify import call_haiku, chunks, parse_response   # reuse the batching idiom
from config import (
    BATCH_SIZE,
    FOLLOWUP_FEATURE_FALLBACK,
    FOLLOWUP_FEATURE_MODEL,
    FOLLOWUP_FEATURE_SPEC,
    FOLLOWUP_PROMPT_VERSION,
)
from db import connect
from followup_features import EXTRACTOR_VERSION

PROMPT_PATH = os.path.join(os.path.dirname(__file__), "prompts", "followup_feature.txt")
HAIKU_COST_PER_1K = 0.20   # ~USD per 1,000 messages (cost reference, Haiku 4.5)


def build_feature_block() -> str:
    out = []
    for dim, vals in FOLLOWUP_FEATURE_SPEC.items():
        out.append(f"{dim.upper()} — choose exactly one:")
        out += [f"- {v}: {d}" for v, d in vals.items()]
        out.append("")
    return "\n".join(out).strip()


def build_system_prompt() -> str:
    template = open(PROMPT_PATH, encoding="utf-8").read()
    return template.replace("{feature_block}", build_feature_block())


def format_batch_user_message(batch: list[dict]) -> str:
    lines = [f"Label these {len(batch)} follow-up messages:", ""]
    for i, row in enumerate(batch, 1):
        msg = (row.get("followup_new_text") or "").replace("\n", " ").strip()
        lines.append(f"[{i}] MESSAGE: {msg}")
    return "\n".join(lines)


def coerce_features(item: dict) -> dict:
    """Validate one model item against the closed enums; unknown/missing -> fallback.
    Pure (no I/O) so it is unit-testable."""
    out = {}
    for dim, vals in FOLLOWUP_FEATURE_SPEC.items():
        v = str(item.get(dim, "") or "").strip().lower()
        out[dim] = v if v in vals else FOLLOWUP_FEATURE_FALLBACK[dim]
    return out


UPDATE_SQL = """
update followup_message_features f set
  hook_type = v.hook_type, tone = v.tone, cta_style = v.cta_style,
  personalization = v.personalization, llm_model = v.llm_model,
  llm_prompt_version = v.llm_prompt_version, llm_classified_at = now()
from (values %s) as v(sent_message_id, hook_type, tone, cta_style, personalization, llm_model, llm_prompt_version)
where f.sent_message_id = v.sent_message_id
"""


def load_rows(cur, retag: bool, limit: int | None) -> list[dict]:
    where = ["extractor_version = %s", "boundary_detected",
             "coalesce(btrim(followup_new_text), '') <> ''"]
    params: list = [EXTRACTOR_VERSION]
    if not retag:
        where.append("(llm_classified_at is null or llm_prompt_version is distinct from %s)")
        params.append(FOLLOWUP_PROMPT_VERSION)
    sql = (f"select sent_message_id, followup_new_text from followup_message_features "
           f"where {' and '.join(where)} order by sent_message_id")
    if limit:
        sql += f" limit {int(limit)}"
    cur.execute(sql, params)
    return [{"sent_message_id": r[0], "followup_new_text": r[1]} for r in cur.fetchall()]


def main(limit: int | None = None, dry_run: bool = False,
         yes: bool = False, retag: bool = False) -> None:
    load_dotenv()
    system_prompt = build_system_prompt()

    conn = connect()
    cur = conn.cursor()
    rows = load_rows(cur, retag=retag, limit=limit)
    n = len(rows)

    if dry_run:
        print("=== SYSTEM PROMPT ===")
        print(system_prompt)
        print("\n=== FIRST BATCH (user message) ===")
        print(format_batch_user_message(rows[:BATCH_SIZE]) if rows else "(no rows to tag)")
        print(f"\n[dry-run] {n} follow-ups would be tagged with {FOLLOWUP_FEATURE_MODEL}. No API call made.")
        conn.close()
        return

    if n == 0:
        print("Nothing to tag (all matching follow-ups already tagged at the current prompt version).")
        conn.close()
        return

    est = n / 1000 * HAIKU_COST_PER_1K
    print(f"Tagging {n} follow-ups with {FOLLOWUP_FEATURE_MODEL} "
          f"(prompt {FOLLOWUP_PROMPT_VERSION}); est ~${est:.2f}.")
    if not yes:
        if input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Aborted.")
            conn.close()
            return

    from anthropic import Anthropic
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    tagged = failed = missing = 0
    for bnum, batch in enumerate(chunks(rows, BATCH_SIZE), 1):
        user_message = format_batch_user_message(batch)
        try:
            raw = call_haiku(client, system_prompt, user_message, model=FOLLOWUP_FEATURE_MODEL)
            parsed = parse_response(raw)
        except Exception as e:
            failed += len(batch)
            print(f"  batch {bnum} failed ({type(e).__name__}: {str(e)[:120]}) — left untagged")
            continue

        by_id = {item["id"]: item for item in parsed if isinstance(item, dict) and "id" in item}
        update_rows = []
        for i, row in enumerate(batch, 1):
            item = by_id.get(i)
            if item is None:
                missing += 1
                continue
            f = coerce_features(item)
            update_rows.append((row["sent_message_id"], f["hook_type"], f["tone"],
                                f["cta_style"], f["personalization"],
                                FOLLOWUP_FEATURE_MODEL, FOLLOWUP_PROMPT_VERSION))
        if update_rows:
            execute_values(cur, UPDATE_SQL, update_rows, template="(%s,%s,%s,%s,%s,%s,%s)")
            conn.commit()              # incremental: progress survives a mid-run crash
            tagged += len(update_rows)
        if bnum % 10 == 0 or bnum * BATCH_SIZE >= n:
            print(f"  batch {bnum}: {tagged}/{n} tagged")

    print(f"Done. Tagged {tagged}; missing-in-response {missing}; failed-batch {failed}. "
          f"(model={FOLLOWUP_FEATURE_MODEL}, prompt_version={FOLLOWUP_PROMPT_VERSION})")
    conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="cap rows (sample for validation)")
    ap.add_argument("--dry-run", action="store_true", help="print prompt + first batch, no API call")
    ap.add_argument("--yes", action="store_true", help="skip the cost confirmation prompt")
    ap.add_argument("--retag", action="store_true", help="re-tag all rows, not just untagged/stale")
    args = ap.parse_args()
    main(limit=args.limit, dry_run=args.dry_run, yes=args.yes, retag=args.retag)
