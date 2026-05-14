# CLAUDE.md

Guidance for Claude Code working in this repo.

## What this project does

Python pipeline that pulls cold-outbound email replies from Instantly, classifies each reply into a fixed taxonomy via Claude Haiku, and surfaces the per-lead status to a non-technical end-user through **NocoDB** (which reads a Postgres materialized view). Excel export still exists but NocoDB is the primary GUI.

## Data flow

```
Instantly API
   │
   │  instantly_sync.py (run.py sync)
   ▼
replies              ◄── one row per received email
   │
   │  classify.py (run.py classify)        Haiku 4.5, batch 25, prompt v3
   ▼
classifications      ◄── multiple rows per reply allowed (one per prompt_version)
   │
   │  leads_status_update.py (run.py update-status)
   │     reuses excel_writer.fetch_per_lead_summary
   │     picks LATEST classification per reply by classified_at desc
   │     so v2 + v3 rows coexist; newest wins automatically
   ▼
leads                ◄── one row per lead_email; columns: status1..status4,
                         auto_status, manual_status, score, reason, clients, campaigns
   │
   │  refresh materialized view lead_status_mv
   ▼
lead_status_mv       ◄── what NocoDB shows the client (joins leads + lead_contacts)
```

`lead_contacts` holds Apollo enrichment (name, title, industry, etc.), uploaded via `run.py upload-leads <file>`.

## Key invariants

- **Fixed label taxonomy** in `config.py` (11 labels: `booked, interested, interested_past, not_now, not_interested, wrong_person, no_longer_there, customer_service, unsubscribe, oof, other`). Don't add/rename without updating `LABEL_DEFINITIONS`, `prompts/classifier.txt`, and re-validating.
- **Idempotent sync**: `replies.instantly_message_id` is unique; upsert on conflict do nothing. `sync_state.last_synced_at` drives incremental pulls.
- **Manual overrides untouched**: `leads.manual_status` and `leads.notes` are never written by automation. `coalesce(manual_status, auto_status)` is what the MV exposes as `status`.
- **Multiple `prompt_version`s coexist** in `classifications`. `excel_writer.fetch_per_lead_summary` orders by `classified_at` ascending and dict-merges so the newest row wins. Do NOT filter by a single `prompt_version` — that breaks reclassify history.
- **Bump `PROMPT_VERSION` in `config.py` for any prompt change** so old/new are diff-able. Currently `v3`.
- **Excluded senders** (config.py `is_excluded_sender`) — bots, internal addresses, do-not-reply prefixes — are dropped at writeback time, never classified out.

## Subsystems

| File | Role |
|---|---|
| `instantly_sync.py` | Pulls received emails from Instantly v2 API, upserts into `replies` |
| `classify.py` | Cleans bodies, promo-filters (rule-based → `other`), batches to Haiku, writes `classifications`. Failures go to `classification_errors`. |
| `prompts/classifier.txt` | System prompt; rule 5 dictates the format of `reason` (currently: plain-English ≤20 words, with sub-category required for `other`). |
| `leads_status_update.py` | Materializes per-lead status onto `leads` (used by both Excel export and the MV). |
| `excel_writer.py` | Two modes: `export_writeback` (update existing xlsx) and `export_fresh`. `fetch_per_lead_summary` is shared with `leads_status_update`. |
| `db.py` | Direct psycopg2 connection (for ops PostgREST can't do, e.g. `refresh materialized view concurrently`). |
| `lead_contacts_upload.py` | Upserts Apollo enrichment CSV/xlsx into `lead_contacts`. |
| `resolve_company_names.py` | LLM-resolves ambiguous company names where Apollo and source-list disagree. |
| `backfill_lead_status.py` | Pulls per-lead `interest_status` from Instantly `/leads/list` into `replies.lead_status`. |
| `smartscout_upload.py` | Upserts SmartScout Amazon brand market data CSV/xlsx into `smartscout_brands`. |
| `smartscout_resolve.py` | Fuzzy-matches lead companies to SmartScout brands (≥92 → `fuzzy`). Auto-runs at the end of `upload-leads`. |
| `smartscout_llm_resolve.py` | LLM (Haiku) second pass on grey-zone leads (default 85–92). Manual only — has `--dry-run` and a y/N prompt. Writes incrementally per batch. |
| `prospeo_sync.py` | **Separate pipeline** — pulls new decision-maker leads from Prospeo for inclusion-list domains, two-stage filter (rules + Haiku LLM), writes to `prospeo_new_leads` table + `exports/*.xlsx` for Jam. Title list is owner-only (CEO/Founder/Owner/President + variants). Has `--max-credits` budget cap; always use it. |
| `scripts/prospeo_category_pilot.py` | One-off pilot script that compares domain-mode vs category-mode Prospeo searches. Read-only (writes XLSX only, never touches DB). See `FINDINGS.html` for the empirically-verified filter shape and accepted industry strings. |
| `scripts/verify_claims.py`, `scripts/title_analysis.py`, `scripts/verify_prospeo_shape.py` | Read-only verification scripts. Used to validate every number in `FINDINGS.html` against live data. |
| `migrations.sql` | Source of truth for table DDL. The MV definition (`lead_status_mv`) lives in Supabase only — fetch via `select definition from pg_matviews`. Prospeo tables (`prospeo_new_leads`, `domain_inclusion_list`) are at the bottom. |

## CLI (run.py)

```bash
source venv/Scripts/activate

python run.py sync [--days N]         # incremental pull from Instantly
python run.py refresh-status          # pull per-lead interest_status from Instantly
python run.py classify                # Haiku classify unclassified replies (only)
python run.py update-status           # leads ← classifications, then refresh MV
python run.py refresh [--days N]      # one-shot: sync → refresh-status → classify → update-status
python run.py upload-leads <file>     # Apollo enrichment upsert
python run.py resolve-companies       # LLM company-name resolution
python run.py export ...              # legacy Excel export (writeback or fresh)
python run.py backfill-tags           # backfill replies.tags from Instantly campaign tag mappings
python run.py upload-smartscout <file>           # upsert SmartScout brand market data; sets up brand → market data lookup
python run.py resolve-smartscout [--rerun]       # fuzzy-match leads to brands (auto-runs after upload-leads)
python run.py llm-resolve-smartscout [--min-score N --max-score N --dry-run --yes]   # LLM grey-zone pass (manual; ~$1 per ~3.5k leads)
```

`run.py classify` does NOT forward args. To pass flags, call `classify.py` directly:

```bash
python classify.py --reclassify          # include already-classified replies (creates new rows)
python classify.py --variety --limit 10  # variety-balanced sample (for prompt iteration)
python classify.py --dry-run             # print prompt + 3 cleaned replies, no API call
python classify.py --diff-against v2     # diff current PROMPT_VERSION against v2
```

## Reclassify workflow

When the prompt changes:
1. Bump `PROMPT_VERSION` in `config.py`.
2. Edit `prompts/classifier.txt`.
3. (Optional) Sanity-check on a small sample: `python classify.py --limit 5 --variety --reclassify`.
4. Full reclassify: `python classify.py --reclassify` (~$0.20 / 1,000 replies on Haiku).
5. Push reasons forward: `python run.py update-status` (reads latest classification per reply, writes `leads.reason`, refreshes `lead_status_mv`).
6. NocoDB sees the new reasons in the existing `"More detail about status"` column. No NocoDB sync needed — same column, new content.

Old `prompt_version` rows are kept so you can diff regressions.

## NocoDB notes

- NocoDB caches schema metadata. After any DDL change to `lead_status_mv` (rename column, add column, change type), the user must trigger meta-sync in NocoDB or disconnect/reconnect the data source. Pure data changes (refreshing the MV) don't need a sync.
- The MV columns the client sees are aliased in Title Case with spaces, e.g. `"Source Company Name"`, `"More detail about status"`. Match this convention when adding columns.

## Environment

`.env` requires:
- `INSTANTLY_API_KEY`
- `SUPABASE_URL`, `SUPABASE_KEY` (service role, for PostgREST)
- `SUPABASE_DB_PASSWORD` (or `SUPABASE_DB_URL` override) — for direct psycopg2 ops
- `ANTHROPIC_API_KEY`

Python 3.11+, virtualenv in `venv/`. `requirements.txt` is committed.

## Validation gate

Before any full reclassify, do a stratified hand-review on ~200–500 replies, weighted toward `other` / `wrong_person` / `no_longer_there` / `not_now` (the labels with the highest disagreement risk). Target >90% overall accuracy, >85% on rare labels. Iterate the prompt and bump `PROMPT_VERSION` between runs. `scripts/compare_models.py` exists for Haiku-vs-Sonnet bake-offs (writes test rows tagged `v3-haiku` / `v3-sonnet`; clean up with `delete from classifications where prompt_version in (...)`).

## Cost reference

- Haiku 4.5: ~$0.20 per 1,000 replies (input cached after batch 1).
- Sonnet 4.6: ~$0.60 per 1,000 replies (3× Haiku).
- Instantly v2 API: ~100 req/min typical tier — exponential backoff on 429.

## Things that have bitten before

- `run.py classify` ignores extra args silently — call `classify.py` directly when passing flags.
- Updating `lead_status_mv` requires `drop` + `create` (Postgres can rename columns directly via `alter materialized view ... rename column`, but anything else needs full recreate). The MV definition is NOT in `migrations.sql` — fetch it from `pg_matviews` first.
- `information_schema.columns` does not list materialized view columns in this Postgres version. Use `pg_attribute` to inspect: `select attname from pg_attribute where attrelid = 'public.lead_status_mv'::regclass and attnum > 0 and not attisdropped`.
- After reclassifying, `excel_writer` / `update-status` correctly pick the newest row per reply. Don't introduce code that filters on a single `prompt_version` — it breaks history.
- **Prospeo industry filter**: shape is `filters.company_industry.include` (singular, top-level). Common wrong shapes — `filters.company.industries.include` is silently ignored (accepted as syntax but does nothing), and `filters.industries.include` returns `INVALID_FILTERS`. Industry values must come from Prospeo's post-2023 LinkedIn-style enum — `"Apparel and Fashion"` is rejected, `"Retail Apparel and Fashion"` works. See `FINDINGS.html` §6 for the verified list.
- **Prospeo websites filter** silently ignores entries above 15 (verified after a 506-credit production burn). `PROSPEO_BATCH_DOMAINS = 15` is hard-coded.
- **`fetch_domains_with_decision_maker`** can drop the SSL connection mid-query on the 230k-row scan. Wrapped in 3-attempt retry with fresh connection; if you replace this function, preserve the retry.
