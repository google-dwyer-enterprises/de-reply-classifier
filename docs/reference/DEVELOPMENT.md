# Developer Onboarding

Guide for a new engineer picking up this repo. Read `README.md` and `CLAUDE.md` after this — they cover the runtime architecture in depth.

## 1. What this project does (60-second version)

A Python pipeline that:

1. **Syncs** cold-outbound email replies from Instantly into Postgres (`replies` table).
2. **Classifies** each reply into a fixed 11-label taxonomy via Claude Haiku 4.5 (`classifications` table).
3. **Materializes** the latest classification per lead into a `leads` table and a `lead_status_mv` materialized view.
4. **Surfaces** the per-lead status to a non-technical end-user via **NocoDB** (which reads `lead_status_mv`).

Other modules: Apollo enrichment upload (`lead_contacts`), SmartScout brand market data upload + fuzzy/LLM lead↔brand matching, Prospeo lead scraper, name-extraction backfill.

Storage: **Supabase Postgres**. GUI: **NocoDB**. LLM: **Anthropic Claude Haiku 4.5**.

## 2. Prerequisites

- **Python 3.11+**
- **Git**
- A `.env` with credentials (see §3). You'll need access to:
  - Instantly account (API key)
  - Anthropic Console (API key)
  - Supabase project (URL, service-role key, DB password)
  - Prospeo account (only if working on the scraper)

If you don't have any of those, ask the owner — do not create new accounts/projects.

## 3. Local setup

```bash
git clone https://github.com/desupport515/de-reply-classifier.git
cd de-reply-classifier

python -m venv venv
# Windows
source venv/Scripts/activate
# macOS / Linux
# source venv/bin/activate

pip install -r requirements.txt

cp .env.example .env
# then fill in real values — DO NOT commit .env
```

Verify the DB connection:

```bash
python -c "from db import get_conn; c=get_conn(); print('OK'); c.close()"
```

If that prints `OK`, you're connected to Supabase Postgres.

## 3a. Local directories

You don't have to create any directories by hand. The code creates what it needs:

- `debug/` — auto-created by `instantly_sync.py` for API response dumps.
- `exports/` — auto-created by `prospeo_sync.py` (and any path passed to `excel_writer`).

Optional:

- `original_data/` — **not** auto-created. The docs in `COMMANDS.md` reference paths like `original_data/leads.csv` and `original_data/brands-seller.csv` for Apollo + SmartScout uploads. If you want to follow those examples verbatim, `mkdir original_data` and drop your CSVs there. Otherwise just pass whatever path you want to `run.py upload-leads <file>` / `upload-smartscout <file>`.

All three are in `.gitignore` — never commit their contents.

## 4. Run the pipeline end-to-end

The CLI entry point is `run.py`. The full daily refresh is one command:

```bash
python run.py refresh
# = sync → refresh-status → classify → update-status (+ MV refresh)
```

Or step-by-step while you're learning:

```bash
python run.py sync --days 7        # pull recent replies from Instantly
python run.py refresh-status       # pull per-lead interest_status from Instantly
python run.py classify             # Haiku-classify only the unclassified replies
python run.py update-status        # write latest classification → leads → refresh MV
```

After `update-status`, NocoDB shows the new statuses. Materialized view refresh happens automatically inside `update-status`.

See `COMMANDS.md` for the full CLI reference (Apollo upload, SmartScout, Prospeo, etc.).

## 5. Codebase tour

| File | What it does | When you'll touch it |
|---|---|---|
| `run.py` | CLI dispatcher | Adding a new subcommand |
| `config.py` | Labels, prompt version, excluded senders, env loading | Changing taxonomy or prompt version |
| `db.py` | psycopg2 connection helper | Almost never |
| `instantly_sync.py` | Instantly v2 API → `replies` | Instantly API changes / new fields |
| `classify.py` | Body cleaning, promo filter, batch Haiku call, writes `classifications` | Prompt iteration, accuracy work |
| `prompts/classifier.txt` | The system prompt | Same — bump `PROMPT_VERSION` when you edit it |
| `leads_status_update.py` | `classifications` → `leads` + MV refresh | Status logic changes |
| `excel_writer.py` | Legacy xlsx export; `fetch_per_lead_summary` shared with `leads_status_update` | Touch carefully — shared helper |
| `lead_contacts_upload.py` | Apollo CSV/xlsx upsert into `lead_contacts` | New Apollo fields |
| `prospeo_sync.py` | Prospeo lead scraper | Scraper work |
| `name_extraction.py`, `backfill_names_from_replies.py` | LLM name extraction from reply bodies | Name-quality work |
| `smartscout_upload.py`, `smartscout_resolve.py`, `smartscout_llm_resolve.py` | SmartScout brand data + fuzzy/LLM matching | SmartScout work |
| `resolve_company_names.py` | LLM resolves Apollo vs source company name conflicts | Rare |
| `backfill_lead_status.py`, `backfill_tags.py` | One-shot backfills | When data drifts |
| `migrations.sql` | DDL source of truth | New tables/columns |
| `scripts/` | Ad-hoc tools (model comparison, label scoring, handoff zip, etc.) | Experiments |

## 6. Critical invariants (don't break these)

These come from `CLAUDE.md` — read it for the full rationale.

1. **Fixed 11-label taxonomy** in `config.py`. Changing labels requires updating `LABEL_DEFINITIONS`, `prompts/classifier.txt`, and re-validating on a labeled sample.
2. **Idempotent sync** — `replies.instantly_message_id` is unique; upsert on conflict do nothing.
3. **Never write to `leads.manual_status` or `leads.notes` from automation.** Those are the end-user's manual overrides. The MV exposes `coalesce(manual_status, auto_status)` as `status`.
4. **Multiple `prompt_version`s coexist in `classifications`.** `fetch_per_lead_summary` orders by `classified_at` ascending and dict-merges so the newest row wins. **Do not filter by a single `prompt_version`** — it breaks reclassify history.
5. **Bump `PROMPT_VERSION` in `config.py` whenever you edit `prompts/classifier.txt`.** Currently `v3`.
6. **Excluded senders** (`config.py:is_excluded_sender`) are dropped at writeback time, never classified out. Add new patterns there, not in the prompt.

## 7. Prompt iteration workflow

When you want to change the classifier:

1. Bump `PROMPT_VERSION` in `config.py` (e.g. `v3` → `v4`).
2. Edit `prompts/classifier.txt`.
3. Sanity-check on a small variety sample:
   ```bash
   python classify.py --limit 5 --variety --reclassify --dry-run
   python classify.py --limit 5 --variety --reclassify
   ```
4. Hand-review ~200–500 replies, weighted toward the rare/ambiguous labels (`other`, `wrong_person`, `no_longer_there`, `not_now`). Target >90% overall, >85% on rare labels.
5. Full reclassify: `python classify.py --reclassify` (~$0.20 / 1,000 replies on Haiku).
6. Push forward: `python run.py update-status`.

`run.py classify` does **not** forward extra args. To pass `--reclassify`, `--variety`, `--limit`, `--dry-run`, `--diff-against`, call `classify.py` directly.

## 8. Database tips

- Tables are defined in `migrations.sql`. Run new DDL via Supabase SQL editor.
- The MV definition (`lead_status_mv`) is **not** in `migrations.sql` — fetch it from `pg_matviews`:
  ```sql
  select definition from pg_matviews where matviewname = 'lead_status_mv';
  ```
- `information_schema.columns` doesn't list MV columns in this Postgres version. Use `pg_attribute`:
  ```sql
  select attname from pg_attribute
  where attrelid = 'public.lead_status_mv'::regclass
    and attnum > 0 and not attisdropped;
  ```
- MV column renames work via `alter materialized view ... rename column`; anything else (type change, new column) needs `drop` + `create`.

## 9. NocoDB notes

- After a DDL change to `lead_status_mv`, the user must trigger meta-sync in NocoDB (or disconnect/reconnect the data source). Pure data refreshes don't need that.
- Client-facing column names are Title Case with spaces (e.g. `"Source Company Name"`, `"More detail about status"`). Match this when adding columns.

## 10. Costs (so you don't surprise the owner)

- Haiku 4.5: **~$0.20 / 1,000 replies**.
- Sonnet 4.6: **~$0.60 / 1,000 replies** (3× Haiku).
- Full reclassify of the corpus is cheap; don't be afraid to iterate.
- `python run.py llm-resolve-smartscout` is ~$1 per ~3.5k leads — has `--dry-run` and a y/N prompt; use them.

## 11. Things that have bitten people before

- `run.py classify` silently ignores extra args — call `classify.py` directly when passing flags.
- Filtering `classifications` by a single `prompt_version` breaks reclassify history. Don't.
- Renaming/changing MV columns: pure rename via `alter`, otherwise `drop` + `create`. The MV definition lives only in Supabase — fetch from `pg_matviews` first.
- Don't commit `.env`, lead data (`test_data/`, `original_data/`, `exports/`), logs (`debug/`), or generated reports (`*.html`). The `.gitignore` already excludes these — don't bypass it.

## 12. Where to find help

- `CLAUDE.md` — architecture, invariants, gotchas (this is the single source of truth).
- `COMMANDS.md` — full CLI reference.
- `README.md` — high-level overview.
- `plan*.md` files at the project root (not in this repo) — historical planning context; ask the owner if you need them.

## 13. First-week suggested tasks

To get a feel for the system without risk:

1. Run `python run.py refresh` on a fresh `.env` and watch a full cycle complete.
2. Open Supabase → `replies`, `classifications`, `leads`, `lead_status_mv`. Trace one lead from `replies.from_email` all the way to `lead_status_mv.status`.
3. Open NocoDB and find the same lead from step 2.
4. Run `python classify.py --variety --limit 10 --dry-run` and read the prompt + the cleaned bodies.
5. Read `scripts/compare_models.py` — it's the bake-off harness for Haiku vs Sonnet.

Once those make sense, you're cleared to take real tickets.
