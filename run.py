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
from followup_tracker_upload import main as followup_tracker_upload_main
from select_winning_replies import main as select_winning_replies_main
from lead_contacts_upload import main as upload_leads_main
from leads_status_update import main as update_status_main
from prospeo_sync import main as prospeo_main
from prospeo_sync import enrich_mobile_for_accepted as prospeo_enrich_mobile
from prospeo_sync import export_all_leads as prospeo_export_all
from resolve_company_names import main as resolve_companies_main
from smartscout_llm_resolve import main as llm_resolve_smartscout_main
from smartscout_resolve import main as resolve_smartscout_main
from smartscout_upload import main as upload_smartscout_main


def run_script(script: str, *script_args: str) -> None:
    """Invoke a sibling script with the same Python interpreter.
    Raises SystemExit on non-zero exit so chaining stops on failure."""
    cmd = [sys.executable, script, *script_args]
    print(f"\n>>> {' '.join(cmd[1:])}\n")
    rc = subprocess.run(cmd).returncode
    if rc != 0:
        sys.exit(f"\n!!! {script} failed with exit code {rc}; aborting.")


def cmd_sync(args) -> None:
    """Run both inbound (received) and outbound (sent) syncs.

    Per FOLLOWUP_ANALYSIS_PLAN.md Phase 2 Edit D — outbound pass
    introduced for the follow-up tracker MV. Inbound pass is the existing
    behavior. Each pass uses its own sync_state cursor when --days is omitted.
    """
    extra = []
    if args.days is not None:
        extra += ["--days", str(args.days)]
    run_script("instantly_sync.py", "--type", "received", *extra)
    run_script("instantly_sync.py", "--type", "sent", *extra)


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
    # followup_tracker_mv / followup_messages_mv are regular views now (converted
    # for NocoDB compatibility), so they auto-recompute on query — no refresh
    # needed here.


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

    ut = sub.add_parser("upload-followup-tracker",
                        help="One-time ingest of Jam's manual follow-up tracker CSV into lead_outcomes")
    ut.add_argument("file", help="Path to followup_tracker_*.csv (typically in original_data/)")

    sw = sub.add_parser("select-winning-replies",
                        help="Identify winning follow-up per booked lead (Option D + D2)")
    sw.add_argument("--dry-run", action="store_true",
                    help="Print selections without writing to DB")
    sw.add_argument("--limit", type=int, default=None,
                    help="Process only N booked leads (for testing)")

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

    us = sub.add_parser("upload-smartscout", help="Upsert SmartScout brand market data CSV/xlsx into smartscout_brands")
    us.add_argument("file", help="Path to .csv or .xlsx (extension optional)")

    rs = sub.add_parser("resolve-smartscout", help="Fuzzy-match leads to SmartScout brands (LLM pass is separate: llm-resolve-smartscout)")
    rs.add_argument("--rerun", action="store_true", help="Re-resolve all leads, not just unresolved")
    rs.add_argument("--limit", type=int, default=None, help="Cap number of leads (testing)")

    sl = sub.add_parser("scrape-leads",
                        help="Pull decision-maker leads from Prospeo (domain or category mode)")
    sl.add_argument("--mode", choices=["domain", "category"], default="domain",
                    help="domain=query Prospeo per inclusion-list domain (default). "
                         "category=query Prospeo by industry, paginate per industry "
                         "with state in category_scrape_state.")
    sl.add_argument("--domains", help="[domain mode] CSV path; defaults to domain_inclusion_list table")
    sl.add_argument("--limit", type=int, default=None,
                    help="[domain mode] Cap number of input domains")
    sl.add_argument("--target-leads", type=int, default=None,
                    help="[category mode] Stop after this many accepted leads. "
                         "Default: unlimited (until budget cap or all industries exhausted).")
    sl.add_argument("--country", default=None,
                    help="[category mode] Comma-separated countries for company_location_search. "
                         "e.g. \"United States,Canada\". Default: no location filter (global).")
    sl.add_argument("--skip-industries", default=None,
                    help="[category mode] Comma-separated industry names to skip this run. "
                         "Useful when an industry has high dupe rate from prior runs. "
                         "Names must match PROSPEO_INDUSTRIES exactly. "
                         "State for skipped industries is preserved untouched.")
    sl.add_argument("--dry-run", action="store_true")
    sl.add_argument("--skip-llm", action="store_true", help="Skip Haiku grey-zone agency/brand classifier")
    sl.add_argument("--with-mobile", action="store_true",
                    help="Enrich accepted leads with mobile (10 credits each)")
    sl.add_argument("--max-credits", type=int, default=None,
                    help="Hard budget cap. Aborts run before spending past this.")

    el = sub.add_parser("export-leads",
                        help="Dump prospeo_new_leads into a fresh CSV + XLSX")
    el.add_argument("--mode", choices=["domain", "category"], default=None,
                    help="Filter export to one scrape_mode. "
                         "Omit to export both modes together (default).")

    em = sub.add_parser("enrich-mobile",
                        help="Catch-up: add mobile numbers to all accepted leads in DB that don't have one yet")
    em.add_argument("--limit", type=int, default=None, help="Cap number of leads to enrich")
    em.add_argument("--dry-run", action="store_true",
                    help="Show how many leads would be enriched and estimated cost; no API calls")

    ll = sub.add_parser("llm-resolve-smartscout",
                        help="LLM-only second pass on grey-zone leads (after resolve-smartscout --skip-llm)")
    ll.add_argument("--min-score", type=float, default=85.0)
    ll.add_argument("--max-score", type=float, default=92.0)
    ll.add_argument("--limit", type=int, default=None)
    ll.add_argument("--yes", action="store_true", help="Skip confirmation")
    ll.add_argument("--dry-run", action="store_true",
                    help="Print cost estimate and exit; no API calls or DB writes")

    args = parser.parse_args()

    if args.command == "export":
        cmd_export(args)
    elif args.command == "upload-leads":
        upload_leads_main(args.file)
    elif args.command == "upload-followup-tracker":
        followup_tracker_upload_main(args.file)
    elif args.command == "select-winning-replies":
        select_winning_replies_main(dry_run=args.dry_run, limit=args.limit)
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
    elif args.command == "upload-smartscout":
        upload_smartscout_main(args.file)
    elif args.command == "resolve-smartscout":
        resolve_smartscout_main(rerun=args.rerun, limit=args.limit)
    elif args.command == "scrape-leads":
        country_list = (
            [c.strip() for c in args.country.split(",") if c.strip()]
            if args.country else None
        )
        skip_list = (
            [s.strip() for s in args.skip_industries.split(",") if s.strip()]
            if args.skip_industries else None
        )
        prospeo_main(mode=args.mode,
                     domains_csv=args.domains, limit=args.limit,
                     target_leads=args.target_leads, country=country_list,
                     skip_industries=skip_list,
                     dry_run=args.dry_run, skip_llm=args.skip_llm,
                     with_mobile=args.with_mobile,
                     max_credits=args.max_credits)
    elif args.command == "enrich-mobile":
        prospeo_enrich_mobile(limit=args.limit, dry_run=args.dry_run)
    elif args.command == "export-leads":
        prospeo_export_all(mode=args.mode)
    elif args.command == "llm-resolve-smartscout":
        llm_resolve_smartscout_main(min_score=args.min_score, max_score=args.max_score,
                                    limit=args.limit, yes=args.yes, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
