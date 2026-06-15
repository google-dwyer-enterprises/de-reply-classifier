# Documentation

Feature documentation, plans, and reports, grouped by area. (Project-level
`CLAUDE.md` and `README.md` stay in the repo root.)

## scraping/ — Lead scraper (Prospeo era + verification)
- [ARCHITECTURE.html](scraping/ARCHITECTURE.html) — scraper architecture & approach
- [FILTERS.html](scraping/FILTERS.html) — filters reference
- [FINDINGS.html](scraping/FINDINGS.html) — verification & updated findings (industry/filter shapes)
- [LEAD_SCRAPER_REPORT.html](scraping/LEAD_SCRAPER_REPORT.html) — verification, pilot results & updates
- [PILOT_REPORT.html](scraping/PILOT_REPORT.html) — category vs domain pilot
- [PROSPEO.html](scraping/PROSPEO.html) — Prospeo lead scraper

## lead-quality/ — Lead quality, reseller detection, QA
- [BETTERCONTACT_LEAD_QUALITY_PLAN.md](lead-quality/BETTERCONTACT_LEAD_QUALITY_PLAN.md) — BetterContact quality remediation
- [RESELLER_DETECTION_PLAN.md](lead-quality/RESELLER_DETECTION_PLAN.md) / [.html](lead-quality/RESELLER_DETECTION_PLAN.html) — reseller detection funnel
- [QA_LAYERS_REPORT.html](lead-quality/QA_LAYERS_REPORT.html) — every criterion, before & after the QA layers
- [ROADMAP_IMPLEMENTATION_PLAN.md](lead-quality/ROADMAP_IMPLEMENTATION_PLAN.md) / [.html](lead-quality/ROADMAP_IMPLEMENTATION_PLAN.html) — automating the gap-fixes

## cost/ — Cost optimization
- [COST_RESEQUENCING_PLAN.md](cost/COST_RESEQUENCING_PLAN.md) / [.html](cost/COST_RESEQUENCING_PLAN.html) — pay less for the same/better leads
- [COST_RESEQ_BEFORE_AFTER.html](cost/COST_RESEQ_BEFORE_AFTER.html) — before & after comparison

## reviewer/ — Lead-reviewer portal & scrape automation
- [LEAD_AUTOMATION.md](reviewer/LEAD_AUTOMATION.md) — operator guide
- [LEAD_AUTOMATION_PLAN.html](reviewer/LEAD_AUTOMATION_PLAN.html) — implementation plan
- [LEAD_AUTOMATION_MOCKUPS.html](reviewer/LEAD_AUTOMATION_MOCKUPS.html) — UI mockups
- [LEAD_REVIEWER_PLAN.html](reviewer/LEAD_REVIEWER_PLAN.html) — static-page replacement plan
- [REVIEW_BATCH_LIFECYCLE.html](reviewer/REVIEW_BATCH_LIFECYCLE.html) — per-batch review lifecycle + cleanup
- [REVIEW_BATCH_OPTIONS_MOCKUPS.html](reviewer/REVIEW_BATCH_OPTIONS_MOCKUPS.html) — review-batch scoping mockups

## replies/ — Reply classification & follow-up analysis
- [FOLLOWUP_ANALYSIS.html](replies/FOLLOWUP_ANALYSIS.html) — follow-up tracker v3 (spreadsheet shape)
- [FOLLOWUP_ANALYSIS_PLAN.md](replies/FOLLOWUP_ANALYSIS_PLAN.md) — follow-up tracker plan v3
- [LEAD_CLEANING_PLAN.md](replies/LEAD_CLEANING_PLAN.md) — booked-count fix, names, reply tracking
- [TRACKER_EMPTY_CELLS.html](replies/TRACKER_EMPTY_CELLS.html) — which tracker cells are blank and why
- [FOLLOWUP_EFFECTIVENESS_PLAN.md](replies/FOLLOWUP_EFFECTIVENESS_PLAN.md) / [.html](replies/FOLLOWUP_EFFECTIVENESS_PLAN.html) — descriptive "which follow-ups are working" cross-lead analysis plan (NocoDB view + HTML report)
- [FOLLOWUP_EFFECTIVENESS.html](replies/FOLLOWUP_EFFECTIVENESS.html) — the descriptive "which follow-ups are working" report (deterministic features; NocoDB views `followup_patterns_mv` / `followup_timing_mv`)

## reference/ — Developer reference
- [DEVELOPMENT.md](reference/DEVELOPMENT.md) — developer onboarding
- [COMMANDS.md](reference/COMMANDS.md) — commands cheat sheet
- [COMPANY_RESOLUTION.md](reference/COMPANY_RESOLUTION.md) — company resolution developer guide
- [ADDING_A_NOCODB_VIEW.md](reference/ADDING_A_NOCODB_VIEW.md) — runbook: surface new data to the client as a NocoDB view (plain vs. materialized, conventions, gotchas)
