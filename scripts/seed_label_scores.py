"""Seed / refresh the label_scores table from config.LABEL_SCORES.

Run once after creating the table, and again any time LABEL_SCORES changes
in config.py. Idempotent — safe to rerun.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import LABEL_SCORES
from excel_writer import get_supabase


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    supabase = get_supabase()
    rows = [{"label": k, "score": v} for k, v in LABEL_SCORES.items()]
    supabase.table("label_scores").upsert(rows, on_conflict="label").execute()

    print(f"Upserted {len(rows)} rows into label_scores:")
    for row in sorted(rows, key=lambda r: -r["score"]):
        print(f"  {row['label']:20} {row['score']:>4}")


if __name__ == "__main__":
    main()
