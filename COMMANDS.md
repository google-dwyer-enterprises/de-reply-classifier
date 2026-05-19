# Commands Cheat Sheet

Think of this project like a kitchen. Each command is a tool. You use them in the right order to cook the meal.

First, turn on the stove (every time you open a new terminal):

```bash
source venv/Scripts/activate
```

---

## The Daily Job (do this most days)

Pull new email replies, label them, and update the leads list.

```bash
python run.py refresh
```

That's it. One command. It does 4 things in order:
1. Get new replies from Instantly (the email tool).
2. Get the latest "interest status" for each lead from Instantly.
3. Read the new replies and tag each one (booked, interested, not_now, etc.).
4. Update the master leads table so NocoDB shows the new info.

If you want to look back further than the default:
```bash
python run.py refresh --days 7
```

---

## Adding New Leads (when you get a new Apollo export)

You got a CSV/xlsx of leads from Apollo. Load them into the database:

```bash
python run.py upload-leads <file>
```

Example:
```bash
python run.py upload-leads "original_data/leads.csv"
```

This also **auto-matches** those leads to SmartScout brands at the end. You don't need to do anything extra.

---

## Adding/Updating SmartScout Brands

You got a new SmartScout export of Amazon brand stats:

```bash
python run.py upload-smartscout <file>
```

After uploading new brand data, re-match all leads to the new brand list:

```bash
python run.py resolve-smartscout --rerun
```

(Optional) Use AI to match the leads that fuzzy matching wasn't sure about. Costs about **$1**:

```bash
python run.py llm-resolve-smartscout --dry-run    # see cost first
python run.py llm-resolve-smartscout --yes        # do it for real
```

---

## The Pieces of `refresh` (if you want to run them one by one)

You almost never need these alone. They're inside `refresh` already.

```bash
python run.py sync                # 1. get new replies
python run.py refresh-status      # 2. get lead "interest status"
python run.py classify            # 3. label replies with AI
python run.py update-status       # 4. update leads table
```

---

## Getting New Leads from Prospeo (the lead scraper)

A separate pipeline that pulls **new** decision-maker leads from Prospeo, filters out agencies/resellers, and writes a CSV that Jam can load into Instantly. Doesn't touch the main classification flow above.

Two modes — pick one per run via `--mode`:

| Mode | Asks Prospeo for | Dedup is by | Best when |
|---|---|---|---|
| `domain` (default) | "people at THESE 52k inclusion-list domains" | Domain (already in `lead_contacts` / `replies` / `prospeo_new_leads`) | You have a hand-curated target list and want full coverage of those exact companies. |
| `category` | "people in THESE 12 verified industries, in THESE countries" | Email (already in DB) + per-industry pagination cursor | You want to *discover* new DTC brands beyond the curated list. Higher brand rate (~70% pilot-measured vs ~21% domain). |

Both modes share the same downstream filter (rule + Haiku LLM agency check), DB schema (`prospeo_new_leads`), and export shape (Accepted + Rejected XLSX sheets).

### Domain mode (today's behavior — unchanged)

```bash
# Plan-only — see what would be queried, no API calls
python run.py scrape-leads --domains inclusion_clean.csv --dry-run --limit 50

# Live run with a safety cap (recommended) — about $0.04
python run.py scrape-leads --domains inclusion_clean.csv --limit 50 --max-credits 5

# Full weekly drop (input defaults to domain_inclusion_list table)
python run.py scrape-leads --max-credits 500
```

### Category mode (NEW — added 2026-05-18)

```bash
# Plan-only — show the filter that would go to Prospeo, no spend
python run.py scrape-leads --mode category --dry-run --country "United States,Canada"

# $0.20 sanity check — 5 leads
python run.py scrape-leads --mode category --target-leads 5 --max-credits 10 \
    --country "United States,Canada"

# Real weekly run — 100 brand leads target, $4 hard cap
python run.py scrape-leads --mode category --target-leads 100 --max-credits 200 \
    --country "United States,Canada"
```

Category mode is **resumable across runs** via `category_scrape_state`. The 2nd run picks up where the 1st stopped — same pagination cursor per industry — so weekly cron just keeps growing the pool.

**If Prospeo runs out of credits mid-run:** the script aborts with `Prospeo INSUFFICIENT_CREDITS — top up and re-run`. The pagination cursor is **not** touched. Just top up at `prospeo.io/dashboard` and re-run the same command — it resumes cleanly. No SQL cleanup needed.

### Common to both modes

```bash
# Add mobile numbers later for accepted leads only (~$0.20 each)
python run.py enrich-mobile --dry-run    # see cost first
python run.py enrich-mobile              # do it for real

# Dump the cumulative table to CSV + XLSX (no API cost).
# XLSX shows scrape_mode + source_industry so you can audit which path
# produced each row.
python run.py export-leads                    # both modes combined
python run.py export-leads --mode domain      # domain-mode rows only
python run.py export-leads --mode category    # category-mode rows only
```

Filename reflects the filter: `cumulative_<ts>.xlsx` for combined, `cumulative_domain_<ts>.xlsx` or `cumulative_category_<ts>.xlsx` for filtered.

**Flags reference:**

| Flag | Mode | Effect |
|---|---|---|
| `--mode {domain,category}` | both | Default `domain`. |
| `--domains <csv>` | domain | Input CSV. Defaults to `domain_inclusion_list` table. |
| `--limit N` | domain | Cap number of input domains. |
| `--target-leads N` | category | Stop after this many accepted leads. Default unlimited. |
| `--country "X,Y"` | category | Comma-separated country list for `company_location_search`. Verified: `"United States,Canada"`. |
| `--max-credits N` | both | **Hard budget cap.** Use it every time. |
| `--skip-llm` | both | Rule-only filter (no Haiku grey-zone). |
| `--with-mobile` | both | Mobile enrich on accepted leads (10 credits each). |
| `--dry-run` | both | Print filter + exit. No spend. |

**Pilot script** — `scripts/prospeo_category_pilot.py` still exists for ad-hoc analysis (e.g. compare modes side-by-side, probe new industry strings). Production category-mode runs should use `run.py scrape-leads --mode category` instead since it dedupes and writes to DB.

Always run with `--max-credits` to cap spend. See `PROSPEO.html` "Category mode" section for the design + state-table details.

---

## Other Helpful Commands

| Command | What it does |
|---|---|
| `python run.py backfill-tags` | Adds campaign tags to old replies that are missing them |
| `python run.py resolve-companies` | Uses AI to fix confusing company names |
| `python run.py export` | Old way to make an Excel file (NocoDB is the new way) |

---

## Typical Workflows

### Most days
```bash
source venv/Scripts/activate
python run.py refresh
```

### First-time setup (or you have BOTH new brands AND new leads)
**Always brands first, leads second.** `upload-leads` auto-matches against whatever brands are in the database — so brands need to be there first.
```bash
source venv/Scripts/activate
python run.py upload-smartscout "original_data/brands-seller.csv"   # 1. brands first
python run.py upload-smartscout "original_data/brands-vendor.csv"   # 2. brands first
python run.py upload-leads "original_data/leads.csv"         # 3. then leads (auto-matches)
python run.py refresh
```

### Got a new Apollo export (brands already loaded)
```bash
source venv/Scripts/activate
python run.py upload-leads "original_data/new_leads.csv"     # auto-matches at the end
python run.py refresh
```

### Got a new SmartScout export only
```bash
source venv/Scripts/activate
python run.py upload-smartscout "original_data/new_brands.csv"
python run.py resolve-smartscout --rerun                     # re-match all existing leads
python run.py llm-resolve-smartscout --yes                   # optional, costs ~$1
```

### Oops — uploaded leads before brands
```bash
python run.py resolve-smartscout --rerun     # redo the matching
```

### Brand new prompt for the AI labeler
1. Bump `PROMPT_VERSION` in `config.py`.
2. Edit `prompts/classifier.txt`.
3. Re-label everything: `python classify.py --reclassify`
4. Push the new labels to the leads table: `python run.py update-status`
