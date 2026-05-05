"""Orchestrator CLI.

Subcommands:
  export --mode {writeback,fresh} [flags]   — Phase 6 Excel export (legacy)
  upload-leads <file>                       — upsert Apollo enrichment into lead_contacts
  sync [--days N]                           — pull latest replies from Instantly
  classify                                  — classify unclassified replies
  update-status                             — materialize auto_status onto leads
  refresh-status                            — refresh replies.lead_status from Instantly /leads/list
  refresh [--days N]                        — sync → refresh-status → classify → update-status
"""

from __future__ import annotations

import argparse
import subprocess
import sys

from backfill_lead_status import main as backfill_lead_status_main
from backfill_tags import main as backfill_tags_main
from excel_writer import export_fresh, export_writeback
from lead_contacts_upload import main as upload_leads_main
from leads_status_update import main as update_status_main
from resolve_company_names import main as resolve_companies_main


def run_script(script: str, *script_args: str) -> None:
    """Invoke a sibling script with the same Python interpreter.
    Raises SystemExit on non-zero exit so chaining stops on failure."""
    cmd = [sys.executable, script, *script_args]
    print(f"\n>>> {' '.join(cmd[1:])}\n")
    rc = subprocess.run(cmd).returncode
    if rc != 0:
        sys.exit(f"\n!!! {script} failed with exit code {rc}; aborting.")


def cmd_sync(args) -> None:
    extra = []
    if args.days is not None:
        extra += ["--days", str(args.days)]
    run_script("instantly_sync.py", *extra)


def cmd_classify(_args) -> None:
    run_script("classify.py")


def cmd_refresh_status(_args) -> None:
    backfill_lead_status_main([])


def cmd_refresh(args) -> None:
    cmd_sync(args)
    cmd_refresh_status(args)
    cmd_classify(args)
    print("\n>>> update-status\n")
    update_status_main()


def _prompt(question: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{question}{suffix}: ").strip()
    if not val and default is not None:
        return default
    return val


def cmd_export(args) -> None:
    mode = args.mode
    if not mode:
        choice = _prompt(
            "Export mode:\n"
            "  1) writeback - update an existing Excel with status columns\n"
            "  2) fresh - generate a new Excel of classified replies only\n"
            "Choose [1/2]"
        )
        mode = {"1": "writeback", "2": "fresh"}.get(choice)
        if not mode:
            sys.exit("Invalid choice.")

    if mode == "fresh":
        output = args.output or _prompt("Output file path (e.g. replied_leads_20260422.xlsx)")
        if not output:
            sys.exit("Output path is required for fresh mode.")
        export_fresh(output)

    elif mode == "writeback":
        input_path = args.input or _prompt("Input Excel path")
        tab = args.tab or _prompt("Tab name")
        header_row = args.header_row
        if header_row is None:
            header_row = int(_prompt("Header row number", default="1") or "1")
        if not input_path or not tab:
            sys.exit("--input and --tab are required for writeback mode.")
        export_writeback(input_path, tab, header_row, output_path=args.output)

    else:
        sys.exit(f"Unknown mode: {mode}")


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser(prog="run.py")
    sub = parser.add_subparsers(dest="command", required=True)

    ex = sub.add_parser("export", help="Export classifications to Excel")
    ex.add_argument("--mode", choices=["writeback", "fresh"])
    ex.add_argument("--input")
    ex.add_argument("--tab")
    ex.add_argument("--header-row", type=int, default=None)
    ex.add_argument("--output")

    up = sub.add_parser("upload-leads", help="Upsert Apollo enrichment CSV/xlsx into lead_contacts")
    up.add_argument("file", help="Path to .csv or .xlsx (extension optional)")

    sub.add_parser("update-status", help="Materialize auto_status onto leads table from latest non-oof classification")

    sy = sub.add_parser("sync", help="Pull latest replies from Instantly into replies table")
    sy.add_argument("--days", type=int, default=None, help="Lookback window (passes through to instantly_sync.py)")

    sub.add_parser("classify", help="Classify unclassified replies")

    sub.add_parser("backfill-tags", help="Backfill replies/sent_messages.tags from Instantly campaign tag mappings")

    bls = sub.add_parser("backfill-lead-status", help="Backfill replies.lead_status from Instantly per-lead interest status")
    bls.add_argument("--relabel", action="store_true",
                     help="Skip /leads/list pagination; only re-resolve labels from existing lead_status_code")

    sub.add_parser("refresh-status", help="Refresh replies.lead_status from Instantly /leads/list (alias for backfill-lead-status)")

    rf = sub.add_parser("refresh", help="One-shot: sync → refresh-status → classify → update-status")
    rf.add_argument("--days", type=int, default=None, help="Lookback window for sync step")

    rc = sub.add_parser("resolve-companies", help="LLM-resolve ambiguous company names where apollo_company_name ≠ company_name")
    rc.add_argument("--limit", type=int, default=None, help="Cap number of rows (for dry-run)")

    args = parser.parse_args()

    if args.command == "export":
        cmd_export(args)
    elif args.command == "upload-leads":
        upload_leads_main(args.file)
    elif args.command == "update-status":
        update_status_main()
    elif args.command == "sync":
        cmd_sync(args)
    elif args.command == "classify":
        cmd_classify(args)
    elif args.command == "backfill-tags":
        backfill_tags_main()
    elif args.command == "backfill-lead-status":
        backfill_lead_status_main(["--relabel"] if args.relabel else [])
    elif args.command == "refresh-status":
        cmd_refresh_status(args)
    elif args.command == "refresh":
        cmd_refresh(args)
    elif args.command == "resolve-companies":
        resolve_companies_main(limit=args.limit)


if __name__ == "__main__":
    main()
