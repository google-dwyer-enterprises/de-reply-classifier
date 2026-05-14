"""Build a zip of files needed to hand off the Prospeo scraper to a colleague.

Usage: python scripts/make_handoff_zip.py [out.zip]
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FILES = [
    # Handoff docs
    "SESSION_HANDOFF.html",
    "SESSION_HANDOFF.md",
    "PROSPEO.html",
    "PROSPEO.md",
    "CLAUDE.md",
    # Code
    "prospeo_sync.py",
    "run.py",
    "db.py",
    "prompts/agency_filter.txt",
    "scripts/clean_inclusion.py",
    # Schema
    "migrations.sql",
    # Config
    "requirements.txt",
    ".env.example",
    # Data (cleaned lists)
    "original_data/inclusion_clean.csv",
    "original_data/exclusion_clean.csv",
]


def main(out_path: str) -> None:
    out = Path(out_path).resolve()
    missing = []
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in FILES:
            src = ROOT / rel
            if not src.exists():
                missing.append(rel)
                continue
            zf.write(src, arcname=f"prospeo_handoff/{rel}")
            print(f"  + {rel}")
    if missing:
        print("\n!! missing (skipped):", file=sys.stderr)
        for m in missing:
            print(f"   - {m}", file=sys.stderr)
    print(f"\nWritten: {out}  ({out.stat().st_size/1024/1024:.2f} MB)")


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "prospeo_handoff.zip"
    main(out)
