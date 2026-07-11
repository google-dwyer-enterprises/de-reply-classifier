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

import lead_qa
from backfill_lead_status import main as backfill_lead_status_main
from backfill_tags import main as backfill_tags_main
from excel_writer import export_fresh, export_writeback
from followup_tracker_upload import main as followup_tracker_upload_main
from select_winning_replies import main as select_winning_replies_main
from lead_contacts_upload import main as upload_leads_main
from leads_status_update import main as update_status_main
from bettercontact_sync import main as bettercontact_main
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


def cmd_qa_leads(args) -> None:
    """Scan accepted prospeo_new_leads for prohibited/service garbage and report.
    With --fix, quarantine every flagged row (rejected=true) — also remediates
    leads accepted by an older pipeline version."""
    from db import connect
    conn = connect()
    try:
        report = lead_qa.scan_db_accepted(conn, provider=args.provider)
        lead_qa.print_report(report)
        if args.fix:
            n = lead_qa.fix_flagged(conn, report)
            print(f"\n  quarantined {n} flagged lead(s) (rejected=true, "
                  f"method='qa_gate').")
        elif not report["passed"]:
            print("\n  Re-run with --fix to quarantine these before exporting.")
    finally:
        conn.close()


def cmd_export_leads(args) -> None:
    """Gate then export. Blocks if accepted leads fail the QA gate, unless
    --force. Flagged rows must be quarantined (`qa-leads --fix`) first."""
    from db import connect
    conn = connect()
    try:
        report = lead_qa.scan_db_accepted(conn, mode=args.mode)
        lead_qa.print_report(report)
    finally:
        conn.close()
    if not report["passed"] and not args.force:
        lead_qa.enforce(report)  # raises QAGateError with guidance
    elif not report["passed"]:
        print("\n  --force set: exporting despite QA failure (flagged leads "
              "are still included unless quarantined).")
    prospeo_export_all(mode=args.mode)


def cmd_drain_enrich_queue(args) -> None:
    """Manual drain of the tier-3 revenue-first enrichment queue (async
    submit/collect). Submits pending survivors + collects terminated ones,
    looping with a short wait until the queue is fully resolved (nothing pending
    AND nothing in flight), a hard abort, or the wall-clock cap."""
    import os, time
    from dotenv import load_dotenv
    import bettercontact_sync as bc
    from db import connect
    load_dotenv()
    api_key = (os.environ.get("BETTERCONTACT_API_KEY") or "").strip()
    if not api_key:
        sys.exit("BETTERCONTACT_API_KEY not set in env")
    conn = connect()
    try:
        total = {"enriched": 0, "skipped": 0}
        spent = 0.0
        deadline = time.time() + bc.CLI_DRAIN_MAX_WALL_S
        while True:
            d = bc.drain_enrich_queue(
                conn, api_key, scrape_request_id=args.request_id,
                max_credits=args.max_credits, credits_already_spent=spent,
                submit_limit=args.limit)
            total["enriched"] += d["enriched"]
            total["skipped"] += d["skipped"]
            spent += d["credits_spent"]
            print(f"  drain: +{d['enriched']} enriched / {d['skipped']} skipped / "
                  f"{d['submitted']} submitted, {d['still_pending']} pending + "
                  f"{d['in_flight']} in-flight, BC-cr {spent:.0f}"
                  + (f" [{d['aborted_reason']}]" if d.get("aborted_reason") else ""))
            if d.get("aborted_reason"):
                break
            if d["still_pending"] == 0 and d["in_flight"] == 0:
                break
            if args.limit:                       # one bounded pass when --limit set
                break
            if time.time() > deadline:
                print("  wall-clock cap reached — rows remain queued; re-run to finish")
                break
            time.sleep(bc.BC_POLL_INTERVAL_S)
        print(f"\n=== drain done ===\n  enriched: {total['enriched']}\n"
              f"  skipped:  {total['skipped']}\n  BC credits: {spent:.0f}")
    finally:
        conn.close()


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

    sub.add_parser("extract-followup-features",
                   help="Extract deterministic features (quoted-thread-stripped) over manual "
                        "follow-ups into followup_message_features")
    sub.add_parser("refresh-followup-patterns",
                   help="Full descriptive follow-up analysis: extract features → rebuild "
                        "followup_patterns_mv/_timing_mv → regenerate the HTML report")
    sub.add_parser("generate-followup-experiments",
                   help="Interest follow-up A/B: assign arms + generate static/AI follow-up "
                        "variations for new interest replies (run on the daily cron)")
    sub.add_parser("attribute-followup-experiments",
                   help="Interest follow-up A/B: link marked-sent experiments to real sends "
                        "and attribute the reply outcome (run on the daily cron)")

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
                        help="Pull decision-maker leads (Prospeo default; --provider bettercontact)")
    sl.add_argument("--provider", choices=["prospeo", "bettercontact"],
                    default="prospeo",
                    help="Which scraping provider. prospeo (default) supports domain+category. "
                         "bettercontact supports category only (Lead Finder API).")
    sl.add_argument("--mode", choices=["domain", "category"], default="domain",
                    help="[prospeo] domain=query per inclusion-list domain (default). "
                         "category=query by industry, paginate per industry with state in "
                         "category_scrape_state. [bettercontact] only 'category' is supported.")
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
    sl.add_argument("--skip-brand-verify", action="store_true",
                    help="[bettercontact] Skip the reseller-detection layer "
                         "(domain cache + Shopify probe + SmartScout confirm)")
    sl.add_argument("--skip-amazon-qa", action="store_true",
                    help="[bettercontact] Skip the Amazon Revenue QA gate (Rainforest). "
                         "Useful for smoke tests or when Rainforest credits are exhausted.")
    sl.add_argument("--amazon-qa-max-credits", type=int, default=150,
                    help="[bettercontact] Hard Rainforest credit cap for the Amazon QA gate "
                         "across the whole run (default 150). One credit = one new company "
                         "search; cache hits are free. Shadow mode only stamps the verdict; "
                         "set AMAZON_QA_ENFORCE=True in bettercontact_sync.py to auto-drop.")
    sl.add_argument("--with-mobile", action="store_true",
                    help="[prospeo] Enrich accepted leads with mobile (10 credits each)")
    sl.add_argument("--enrichment", choices=["email", "both"], default="email",
                    help="[bettercontact] What to enrich: email (default) or both "
                         "(emails + phones; phones cost 10 credits each, so the "
                         "per-page credit reservation scales 11x)")
    sl.add_argument("--revenue-first", action="store_true",
                    help="[bettercontact] EXPERIMENTAL: discover email-free (free) -> "
                         "ICP/brand/Amazon-revenue gate the company -> enrich only "
                         "survivors. Shifts spend to (cheap) Rainforest. Opt-in; needs "
                         "a supervised validation before it's the default.")
    sl.add_argument("--revenue-floor", type=int, default=None,
                    help="[bettercontact] Amazon revenue keep/drop line in $/yr for this "
                         "run (default 300000). e.g. 1000000 for a $1M-ICP client. The "
                         "SmartScout grey band scales with it.")
    sl.add_argument("--max-credits", type=int, default=None,
                    help="Hard budget cap. Aborts run before spending past this.")
    sl.add_argument("--page-limit", type=int, default=200,
                    help="[bettercontact] Leads per Lead-Finder call (1-200, default 200). "
                         "Lower values are useful for cheap smoke tests.")

    el = sub.add_parser("export-leads",
                        help="Dump prospeo_new_leads into a fresh CSV + XLSX "
                             "(runs the QA gate first)")
    el.add_argument("--mode", choices=["domain", "category"], default=None,
                    help="Filter export to one scrape_mode. "
                         "Omit to export both modes together (default).")
    el.add_argument("--force", action="store_true",
                    help="Export even if the QA gate fails (not recommended).")

    qa = sub.add_parser("qa-leads",
                        help="QA gate: scan accepted leads for prohibited/service "
                             "garbage; --fix quarantines flagged rows")
    qa.add_argument("--provider", choices=["prospeo", "bettercontact"], default=None,
                    help="Scan only one provider's leads (default: all).")
    qa.add_argument("--fix", action="store_true",
                    help="Quarantine flagged rows (rejected=true). Also cleans up "
                         "leads accepted by an older pipeline version.")

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

    lf = sub.add_parser("llm-followup-features",
                        help="LLM-tag manual follow-ups (hook/tone/CTA/personalization); manual, gated, paid")
    lf.add_argument("--limit", type=int, default=None, help="Cap rows (sample for validation)")
    lf.add_argument("--dry-run", action="store_true", help="Print prompt + first batch; no API call")
    lf.add_argument("--yes", action="store_true", help="Skip the cost confirmation prompt")
    lf.add_argument("--retag", action="store_true", help="Re-tag all rows, not just untagged/stale")

    dq = sub.add_parser("drain-enrich-queue",
                        help="Enrich pending revenue-first survivors (tier-3 queue); "
                             "manual catch-up when the worker isn't running or after BC recovers")
    dq.add_argument("--request-id", type=int, default=None,
                    help="Only drain this scrape_request's queued survivors (default: the CLI NULL bucket)")
    dq.add_argument("--max-credits", type=int, default=None,
                    help="BetterContact enrich credit cap for this drain")
    dq.add_argument("--limit", type=int, default=None, help="Cap rows enriched this run")

    args = parser.parse_args()

    # Wrap dispatch so every `run.py <cmd>` (each daily-cron step is one) records
    # a job_run_log row; daily-job failures also escalate to the admin panel +
    # an email. Fail-safe: if the monitor can't import, fall back to a no-op so
    # it can never be the reason a command doesn't run.
    try:
        from job_monitor import job_run
    except Exception:
        from contextlib import contextmanager as _cm

        @_cm
        def job_run(_job):  # type: ignore[misc]
            yield

    with job_run(args.command):
        _dispatch(args)


def _dispatch(args) -> None:
    if args.command == "export":
        cmd_export(args)
    elif args.command == "upload-leads":
        upload_leads_main(args.file)
    elif args.command == "upload-followup-tracker":
        followup_tracker_upload_main(args.file)
    elif args.command == "select-winning-replies":
        select_winning_replies_main(dry_run=args.dry_run, limit=args.limit)
    elif args.command == "extract-followup-features":
        run_script("followup_features.py")
    elif args.command == "refresh-followup-patterns":
        run_script("followup_features.py")
        run_script("scripts/apply_followup_patterns_view.py")
        run_script("scripts/gen_followup_patterns_report.py")
    elif args.command == "generate-followup-experiments":
        import followup_experiments_data as fxd
        from datetime import datetime, timedelta, timezone
        # 30-day lookback (not 14): only interest replies with NO experiment yet
        # are processed (cap-bounded), so a wider window is cheap and lets the
        # daily run self-heal a cron outage of up to ~30 days instead of leaving a
        # permanent gap for replies that aged past the window while the cron was down.
        since = datetime.now(timezone.utc) - timedelta(days=30)
        print(">>> generate-followup-experiments")
        print("created:", fxd.ensure_experiments(None, since, cap=500))
    elif args.command == "attribute-followup-experiments":
        import followup_experiments_attrib as fxa
        print(">>> attribute-followup-experiments")
        print(fxa.attribute())
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
        if args.provider == "bettercontact":
            # BetterContact only supports category mode; other Prospeo-specific
            # args are silently ignored.
            if args.mode != "category":
                sys.exit("--provider bettercontact requires --mode category")
            # Phones bill 10 cr each; without a cap the budget guard is
            # skipped entirely and a single page can burn up to
            # page_limit * 11 credits (2,200 at the default 200).
            if args.enrichment == "both" and args.max_credits is None:
                sys.exit("--enrichment both requires an explicit --max-credits "
                         "(phones cost 10 credits each; an uncapped phones run "
                         "can burn 11x per page)")
            if args.revenue_first and args.max_credits is None:
                sys.exit("--revenue-first requires an explicit --max-credits "
                         "(it spends BetterContact enrich + Rainforest credits)")
            bettercontact_main(mode=args.mode,
                               revenue_first=args.revenue_first,
                               revenue_floor=args.revenue_floor,
                               target_leads=args.target_leads,
                               country=country_list,
                               skip_industries=skip_list,
                               page_limit=args.page_limit,
                               dry_run=args.dry_run,
                               max_credits=args.max_credits,
                               skip_llm=args.skip_llm,
                               skip_brand_verify=args.skip_brand_verify,
                               skip_amazon_qa=args.skip_amazon_qa,
                               amazon_qa_max_credits=args.amazon_qa_max_credits,
                               enrichment=args.enrichment)
        else:
            # Mobile enrichment bills 10 credits per lead; without a cap the
            # per-lead spend is unbounded. Parity with the BetterContact phones
            # guard above — refuse an uncapped mobile run.
            if args.with_mobile and args.max_credits is None:
                sys.exit("--with-mobile requires an explicit --max-credits "
                         "(mobile enrichment costs 10 credits per lead; an "
                         "uncapped run has no spend ceiling)")
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
        cmd_export_leads(args)
    elif args.command == "qa-leads":
        cmd_qa_leads(args)
    elif args.command == "llm-resolve-smartscout":
        llm_resolve_smartscout_main(min_score=args.min_score, max_score=args.max_score,
                                    limit=args.limit, yes=args.yes, dry_run=args.dry_run)
    elif args.command == "llm-followup-features":
        from followup_llm_features import main as llm_followup_features_main
        llm_followup_features_main(limit=args.limit, dry_run=args.dry_run,
                                   yes=args.yes, retag=args.retag)
    elif args.command == "drain-enrich-queue":
        cmd_drain_enrich_queue(args)


if __name__ == "__main__":
    main()
