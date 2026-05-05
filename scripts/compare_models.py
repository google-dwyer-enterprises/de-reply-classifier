"""Haiku 4.5 vs Sonnet 4.6 comparison test.

Picks a stratified sample of already-classified replies (200 by default),
re-classifies them with both models under the current prompt, and prints a
diff so you can decide whether the Sonnet upgrade is worth it before
committing to a full reclassify run.

Usage:
    python scripts/compare_models.py              # 200 rows, default mix
    python scripts/compare_models.py --size 100   # smaller/cheaper

Both runs write to the `classifications` table tagged with distinct
prompt_version values:
    haiku  -> prompt_version = "{PROMPT_VERSION}-haiku"
    sonnet -> prompt_version = "{PROMPT_VERSION}-sonnet"

Existing v2 rows are kept untouched. After the run, the diff shows
every reply where the two models disagree on label.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from pathlib import Path

# Make the project root importable when running as `python scripts/compare_models.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from supabase import create_client

import classify
from config import BATCH_SIZE, PROMPT_VERSION

HAIKU_MODEL = "claude-haiku-4-5"
SONNET_MODEL = "claude-sonnet-4-6"

# Stratified sample weights — tilted toward the labels most likely to expose
# model differences (per plan.md Phase 4 failure modes).
DEFAULT_QUOTAS = {
    "other": 0.25,
    "wrong_person": 0.15,
    "no_longer_there": 0.15,
    "not_now": 0.125,
    "not_interested": 0.125,
    "interested": 0.05,
    "interested_past": 0.05,
    "customer_service": 0.05,
    "unsubscribe": 0.025,
    "booked": 0.025,
    "oof": 0.025,
}


def pick_sample(supabase, size: int, seed: int = 42) -> list[dict]:
    """Stratified pick from the current v2 classifications joined to replies."""
    rng = random.Random(seed)

    # Pull current v2 labels (reply_id, label) — we only stratify on these.
    label_rows = classify._paginate_all(
        supabase.table("classifications")
        .select("reply_id, label")
        .eq("prompt_version", "v2")
    )
    by_label: dict[str, list[int]] = {}
    for row in label_rows:
        by_label.setdefault(row["label"], []).append(row["reply_id"])

    target_ids: list[int] = []
    for label, weight in DEFAULT_QUOTAS.items():
        n = max(1, round(size * weight))
        pool = by_label.get(label, [])
        rng.shuffle(pool)
        target_ids.extend(pool[:n])

    target_ids = list(dict.fromkeys(target_ids))[:size]  # dedupe, cap

    # Fetch full reply rows.
    replies: list[dict] = []
    CHUNK = 200
    for i in range(0, len(target_ids), CHUNK):
        chunk = target_ids[i : i + CHUNK]
        resp = (
            supabase.table("replies")
            .select("id, lead_email, subject, body")
            .in_("id", chunk)
            .execute()
        )
        replies.extend(resp.data or [])
    return replies


def classify_with(anthropic_client, supabase, model: str, version_tag: str, replies: list[dict]) -> None:
    """Run the classifier with a given model + version tag, writing rows to DB."""
    # Monkey-patch the module-level constants classify.py reads when inserting.
    classify.MODEL = model
    classify.PROMPT_VERSION = version_tag
    # classify_batch reads MODEL/PROMPT_VERSION from `config` at module import,
    # so override those references too.
    import config as _cfg
    _cfg.MODEL = model
    _cfg.PROMPT_VERSION = version_tag
    # And update the names already imported into classify's namespace.
    classify.PROMPT_VERSION = version_tag
    classify.MODEL = model  # used for `model` column

    system_prompt = classify.build_system_prompt()
    print(f"\n--- Running {model} as prompt_version='{version_tag}' on {len(replies)} replies ---")
    for i, batch in enumerate(classify.chunks(replies, BATCH_SIZE), 1):
        print(f"  Batch {i} ({len(batch)} replies)")
        classify.classify_batch(anthropic_client, supabase, system_prompt, batch)


def print_diff(supabase, reply_ids: list[int], v_haiku: str, v_sonnet: str) -> None:
    resp = (
        supabase.table("classifications")
        .select("reply_id, lead_email, prompt_version, label, confidence, reason")
        .in_("reply_id", reply_ids)
        .in_("prompt_version", [v_haiku, v_sonnet])
        .execute()
    )
    by_reply: dict[int, dict[str, dict]] = {}
    for row in resp.data or []:
        by_reply.setdefault(row["reply_id"], {})[row["prompt_version"]] = row

    body_resp = (
        supabase.table("replies").select("id, body").in_("id", reply_ids).execute()
    )
    body_by_id = {r["id"]: (r.get("body") or "") for r in (body_resp.data or [])}

    diffs = 0
    paired = 0
    print()
    print("=" * 100)
    print(f"DIFF — {v_haiku} vs {v_sonnet}")
    print("=" * 100)
    for rid in reply_ids:
        rows = by_reply.get(rid, {})
        h = rows.get(v_haiku)
        s = rows.get(v_sonnet)
        if not (h and s):
            continue
        paired += 1
        if h["label"] == s["label"]:
            continue
        diffs += 1
        body_prev = (body_by_id.get(rid, "") or "")[:120].replace("\n", " ")
        print(f"\nreply_id={rid}  email={h['lead_email']}")
        print(f"  body: {body_prev}")
        print(f"  HAIKU : {h['label']:20} conf={float(h['confidence'] or 0):.2f}  reason={h.get('reason') or ''}")
        print(f"  SONNET: {s['label']:20} conf={float(s['confidence'] or 0):.2f}  reason={s.get('reason') or ''}")

    print()
    print("-" * 100)
    pct = (diffs / paired * 100) if paired else 0
    print(f"Disagreements: {diffs}/{paired} ({pct:.1f}%)")
    print("-" * 100)


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser()
    parser.add_argument("--size", type=int, default=200, help="sample size (default 200)")
    parser.add_argument("--seed", type=int, default=42, help="RNG seed for reproducibility")
    parser.add_argument("--diff-only", action="store_true",
                        help="skip classification, just print diff (use after a previous run)")
    args = parser.parse_args()

    load_dotenv()
    supabase = create_client(
        os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"]
    )

    sample = pick_sample(supabase, args.size, seed=args.seed)
    print(f"Sampled {len(sample)} replies (stratified across labels).")
    reply_ids = [r["id"] for r in sample]

    v_haiku = f"{PROMPT_VERSION}-haiku"
    v_sonnet = f"{PROMPT_VERSION}-sonnet"

    if not args.diff_only:
        from anthropic import Anthropic
        anthropic = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        classify_with(anthropic, supabase, HAIKU_MODEL, v_haiku, sample)
        classify_with(anthropic, supabase, SONNET_MODEL, v_sonnet, sample)

    print_diff(supabase, reply_ids, v_haiku, v_sonnet)
    print(f"\nNext step: hand-review the disagreements above and judge which model wins.")
    print(f"Tip: rows where labels match but reasons differ — skim a few to compare reason quality.")


if __name__ == "__main__":
    main()
