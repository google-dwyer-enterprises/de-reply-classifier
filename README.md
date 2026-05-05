# de-reply-classifier

Pipeline that pulls reply emails from Instantly, classifies them with Claude Haiku 4.5, and materializes per-lead status onto Supabase (consumed by NocoDB on Railway).

## Flow

```
Instantly /api/v2/emails       → instantly_sync.py    → replies (Supabase)
Instantly /api/v2/leads/list   → backfill_lead_status → replies.lead_status (Supabase)
replies (unclassified)         → classify.py          → classifications (Supabase)
replies + classifications      → leads_status_update  → leads.status1-4 (Supabase)
                                                      → REFRESH lead_status_mv
                                                      → NocoDB picks up new data
```

## Setup (one-time)

```bash
# Create venv + install
python -m venv venv
source venv/Scripts/activate     # Windows bash
pip install -r requirements.txt
```

`.env` must contain:
```
INSTANTLY_API_KEY=...
SUPABASE_URL=https://<project>.supabase.co
SUPABASE_KEY=<service_role_key>
ANTHROPIC_API_KEY=...
```

## Daily / weekly run

Activate venv first:
```bash
source venv/Scripts/activate
```

### One-shot (recommended)

Runs sync → refresh-status → classify → update-status in order, aborts on failure:

```bash
python run.py refresh
```

Total runtime ~35–45 min (refresh-status dominates at ~30 min).

### Step-by-step (if you want to run pieces individually)

```bash
# 1. Pull latest replies from Instantly into `replies`
#    Default lookback: 7 days. Override with --days N.
python run.py sync
python run.py sync --days 30

# 2. Refresh per-lead Instantly status onto replies
#    Paginates ~80K leads from /leads/list (~30 min, retries on timeout)
python run.py refresh-status

# 3. Classify any new unclassified replies (Haiku 4.5, batches of 25)
python run.py classify

# 4. Materialize status1-4 onto `leads` and refresh `lead_status_mv`
#    NocoDB sees new data automatically once this finishes.
python run.py update-status
```

## What each step writes

| Command | Reads | Writes |
|---|---|---|
| `sync` | Instantly `/emails` | `replies` (insert, idempotent on `instantly_message_id`) |
| `refresh-status` | Instantly `/leads/list`, `/lead-labels` | `replies.lead_status_code`, `replies.lead_status` |
| `classify` | `replies` (where no classification exists) | `classifications` (one row per reply) |
| `update-status` | `replies` + `classifications` | `leads.status1-4`, `leads.score`, `leads.reason`, `leads.clients`, `leads.campaigns`; refreshes `lead_status_mv` |

## Status columns explained

`leads` table after `update-status`:

- **status1** — best classifier label across all replies for the lead, ranked by `STATUS_PRIORITY` (booked > interested > interested_past > not_now > wrong_person > no_longer_there > not_interested > unsubscribe > other > oof > customer_service)
- **status2** — second-best distinct classifier label
- **status3** — third-best distinct classifier label
- **status4** — latest non-null Instantly `lead_status` (what reps tagged in the Instantly Unibox)
- **score** — derived numeric score
- **reason** — short LLM rationale tied to the reply that produced status1

NocoDB reads from the `lead_status` view (which wraps `lead_status_mv`).

## Recreating `lead_status_mv` (matview + wrapper view)

Use this when you need NocoDB to re-detect schema changes (e.g., new columns added to `leads`), or after dropping with `CASCADE` to clean up.

### Drop

```sql
DROP MATERIALIZED VIEW IF EXISTS lead_status_mv CASCADE;
```

`CASCADE` also drops the dependent `lead_status` view and its index.

### Recreate

```sql
CREATE MATERIALIZED VIEW lead_status_mv AS
WITH lc_with_domain AS (
    SELECT lc_1.lead_email,
        lc_1.apollo_company_name,
        lc_1.domains_match,
        lc_1.lead_list_source,
        lc_1.company_name,
        lc_1.full_name,
        lc_1.first_name,
        lc_1.last_name,
        lc_1.emails,
        lc_1.website,
        lc_1.title,
        lc_1.seniority,
        lc_1.departments,
        lc_1.num_employees,
        lc_1.industry,
        lc_1.keywords,
        lc_1.person_linkedin_url,
        lc_1.company_linkedin_url,
        lc_1.facebook_url,
        lc_1.twitter_url,
        lc_1.city,
        lc_1.state,
        lc_1.country,
        lc_1.company_phone_number,
        lc_1.company_address,
        lc_1.company_city,
        lc_1.company_state,
        lc_1.company_country,
        lc_1.seo_description,
        lc_1.technologies,
        lc_1.total_funding,
        lc_1.annual_revenue,
        lc_1.monthly_revenue,
        lc_1.amazon_storefront,
        lc_1.imported_at,
        lc_1.updated_at,
        lc_1.resolved_company_name,
        lc_1.resolved_company_reason,
        lc_1.resolved_at,
        CASE
            WHEN array_length(parts.arr, 1) >= 3
                 AND parts.arr[array_length(parts.arr, 1) - 1] = ANY (ARRAY['co','com','net','org','gov','edu','ac','or'])
                THEN parts.arr[array_length(parts.arr, 1) - 2]
            ELSE parts.arr[array_length(parts.arr, 1) - 1]
        END AS domain_root,
        regexp_replace(
            CASE
                WHEN array_length(parts.arr, 1) >= 3
                     AND parts.arr[array_length(parts.arr, 1) - 1] = ANY (ARRAY['co','com','net','org','gov','edu','ac','or'])
                    THEN parts.arr[array_length(parts.arr, 1) - 2]
                ELSE parts.arr[array_length(parts.arr, 1) - 1]
            END, '[^a-z0-9]', '', 'g') AS domain_norm,
        regexp_replace(lower(COALESCE(lc_1.company_name, '')), '[^a-z0-9]', '', 'g') AS company_norm,
        regexp_replace(lower(COALESCE(lc_1.apollo_company_name, '')), '[^a-z0-9]', '', 'g') AS apollo_norm
    FROM lead_contacts lc_1,
         LATERAL (SELECT string_to_array(lower(split_part(lc_1.lead_email, '@', 2)), '.') AS arr) parts
)
SELECT lc.apollo_company_name AS "Apollo Company Name",
    lc.domains_match AS "Domains Match",
    lc.lead_list_source AS "Lead List Source",
    lc.company_name AS "Company Name",
    CASE
        WHEN NULLIF(TRIM(BOTH FROM lc.resolved_company_name), '') IS NOT NULL THEN lc.resolved_company_name
        WHEN NULLIF(TRIM(BOTH FROM lc.company_name), '') IS NULL AND NULLIF(TRIM(BOTH FROM lc.apollo_company_name), '') IS NULL THEN initcap(lc.domain_root)
        WHEN NULLIF(TRIM(BOTH FROM lc.apollo_company_name), '') IS NULL THEN lc.company_name
        WHEN NULLIF(TRIM(BOTH FROM lc.company_name), '') IS NULL THEN lc.apollo_company_name
        WHEN lower(TRIM(BOTH FROM lc.company_name)) = lower(TRIM(BOTH FROM lc.apollo_company_name)) THEN lc.company_name
        WHEN lc.domain_root = ANY (ARRAY['gmail','yahoo','hotmail','outlook','live','msn','aol','icloud','me','mac','comcast','verizon','att','sbcglobal','bellsouth','cox','charter','earthlink','roadrunner','rr','libero','virgilio','tin','alice','tiscali','orange','free','wanadoo','laposte','sfr','bbox','neuf','t-online','web','gmx','freenet','arcor','btinternet','sky','virgin','ntlworld','talktalk','blueyonder','shaw','rogers','telus','sympatico','videotron','protonmail','proton','zoho','yandex','mail','fastmail','naver','daum','qq','163','126','sina','sohu']) THEN lc.company_name
        WHEN char_length(lc.domain_norm) >= 3 AND char_length(lc.company_norm) >= 3
             AND (lc.company_norm LIKE '%' || lc.domain_norm || '%' OR lc.domain_norm LIKE '%' || lc.company_norm || '%')
            THEN lc.company_name
        WHEN char_length(lc.domain_norm) >= 3 AND char_length(lc.apollo_norm) >= 3
             AND (lc.apollo_norm LIKE '%' || lc.domain_norm || '%' OR lc.domain_norm LIKE '%' || lc.apollo_norm || '%')
            THEN lc.apollo_company_name
        ELSE lc.company_name
    END AS "Use this company",
    lc.full_name AS "Full Name",
    lc.first_name AS "First Name",
    lc.last_name AS "Last Name",
    lc.emails AS "Emails",
    COALESCE(l.manual_status, l.auto_status) AS status,
    l.status1,
    l.status2,
    l.status3,
    l.status4,
    l.score,
    l.clients,
    l.campaigns,
    l.reason AS "More detail about status",
    lc.website AS "Website",
    lc.title AS "Title",
    lc.seniority AS "Seniority",
    lc.departments AS "Departments",
    lc.num_employees AS "# Employees",
    lc.industry AS "Industry",
    lc.keywords AS "Keywords",
    lc.person_linkedin_url AS "Person Linkedin Url",
    lc.company_linkedin_url AS "Company Linkedin Url",
    lc.facebook_url AS "Facebook Url",
    lc.twitter_url AS "Twitter Url",
    lc.city AS "City",
    lc.state AS "State",
    lc.country AS "Country",
    lc.company_phone_number AS "Company Phone Number",
    lc.company_address AS "Company Address",
    lc.company_city AS "Company City",
    lc.company_state AS "Company State",
    lc.company_country AS "Company Country",
    lc.seo_description AS "SEO Description",
    lc.technologies AS "Technologies",
    lc.total_funding AS "Total Funding",
    lc.annual_revenue AS "Annual Revenue/Monthly Revenue",
    lc.monthly_revenue AS "Monthly Revenue",
    lc.amazon_storefront AS "Amazon Storefront",
    lc.lead_email
FROM lc_with_domain lc
LEFT JOIN leads l ON l.lead_email = lc.lead_email;

CREATE UNIQUE INDEX lead_status_mv_lead_email_idx ON lead_status_mv (lead_email);

CREATE VIEW lead_status AS
SELECT
    "Apollo Company Name",
    "Domains Match",
    "Lead List Source",
    "Company Name",
    "Use this company",
    "Full Name",
    "First Name",
    "Last Name",
    "Emails",
    status,
    status1,
    status2,
    status3,
    status4,
    score,
    clients,
    campaigns,
    "More detail about status",
    "Website",
    "Title",
    "Seniority",
    "Departments",
    "# Employees",
    "Industry",
    "Keywords",
    "Person Linkedin Url",
    "Company Linkedin Url",
    "Facebook Url",
    "Twitter Url",
    "City",
    "State",
    "Country",
    "Company Phone Number",
    "Company Address",
    "Company City",
    "Company State",
    "Company Country",
    "SEO Description",
    "Technologies",
    "Total Funding",
    "Annual Revenue/Monthly Revenue",
    "Monthly Revenue",
    "Amazon Storefront",
    lead_email
FROM lead_status_mv;
```

After recreating, run `python run.py update-status` to populate it (or `REFRESH MATERIALIZED VIEW lead_status_mv;` if data is already in `leads`). Then in NocoDB: delete the `lead_status` table mapping and **Sync Now** to pick up new columns.

## Validation

After `update-status`, sanity-check the booked count matches Instantly Unibox:

```sql
-- Should roughly match Instantly Unibox "Booked-family" total
select count(*) from leads
where status4 in (
  'Meeting booked','Dayly booked','Booked - Dayly','Booked - DE SALES',
  'Booked - Velocity','Booked - FDC','Booked - Sellervue','Booked - Ripple',
  'Booked - Zonlabs','PP - Booked','EC - Booked','AMZPPC - Booked',
  'Epic - Booked','SOKO - Booked','BG - Booked','MOD Booked +ve',
  'Navira - Booked','Lumian - Booked','MOD Booked -ve'
);

-- Per-status4 distribution
select status4, count(*) from leads
where status4 is not null
group by status4 order by 2 desc;

-- Leads classifier flagged as booked but reps haven't tagged in Instantly
select lead_email, status1, status4 from leads
where status1='booked' and (status4 is null or status4 not like '%Booked%')
limit 20;
```

Note: `status4` will undercount Instantly's true booked total because `leads` only contains lead emails that have replied at least once. Reps can tag a lead "Booked" in Instantly without an email reply (call-booked, manual move).

## Deploying as a Railway cron job

Run the whole pipeline on a schedule from the same Railway project that hosts NocoDB.

### Files in repo

- `Dockerfile` — Python 3.11-slim base, installs `requirements.txt`, copies the repo
- `railway.json` — tells Railway to use the Dockerfile and run `python run.py refresh`
- `.dockerignore` — excludes `venv/`, `nocodb_data/`, `exports/`, `.env`, etc.

### One-time setup

1. **Push the repo to GitHub** (Railway pulls from there):
   ```bash
   git add Dockerfile railway.json .dockerignore
   git commit -m "Add Railway deployment config"
   git push
   ```

2. **Create the service in Railway:**
   - Open your existing Railway project (the one with NocoDB)
   - Click **+ New** → **GitHub Repo** → select `de-reply-classifier`
   - Railway auto-detects the Dockerfile and starts building

3. **Add environment variables** (Service → Settings → Variables):
   ```
   INSTANTLY_API_KEY=...
   SUPABASE_URL=https://<project>.supabase.co
   SUPABASE_KEY=<service_role_key>
   ANTHROPIC_API_KEY=...
   ```

4. **Set the cron schedule** (Service → Settings → Cron Schedule):
   ```
   0 6 * * 1
   ```
   (Mon 6am UTC. Adjust to your TZ — e.g., `0 11 * * 1` for 6am EST.)

5. **Disable public networking** (Service → Settings → Networking):
   It's a worker, no port to expose. Toggle off "Generate Domain" if auto-created.

### Verify

- **Trigger a manual run**: Deployments tab → latest deployment → **Restart**
- **Watch logs**: should see `sync → refresh-status → classify → update-status` output
- **Total runtime**: ~35–45 min (refresh-status dominates at ~30 min)

### Cost

Railway charges by runtime. ~45 min/week ≈ 3 hours/month, easily within the Hobby plan's $5 included credit. Likely $0 extra beyond the existing NocoDB service.

### Caveats

- `.env` is excluded from the Docker image; env vars come from Railway's Variables tab.
- Logs live in the Deployments tab. To get Slack alerts on failure, wrap the start command in a script that posts to a webhook on non-zero exit.
- If you need to change the schedule or model, push a new commit — Railway redeploys automatically.

## Other commands

```bash
# Re-resolve replies.lead_status from existing lead_status_code
# (use after adding new label mappings; skips /leads/list pagination)
python run.py backfill-lead-status --relabel

# Backfill replies/sent_messages.tags from Instantly campaign tag mappings
python run.py backfill-tags

# LLM-resolve ambiguous company names (apollo_company_name ≠ company_name)
python run.py resolve-companies
python run.py resolve-companies --limit 50   # dry-run

# Upsert Apollo enrichment file into lead_contacts
python run.py upload-leads path/to/file.xlsx

# Excel export (legacy / hand-review path)
python run.py export --mode fresh --output replied_YYYYMMDD.xlsx
python run.py export --mode writeback --input sheet.xlsx --tab Sheet1 --header-row 1
```

## Failure modes & recovery

- **Sync fails partway** — idempotent. Just rerun `python run.py sync`.
- **Refresh-status times out on a page** — script retries 8x with exponential backoff on ReadTimeout/ConnectionError. If it still dies, rerun from scratch (no incremental cursor persistence; ~30 min lost).
- **Classify quota exceeded** — rerun; only unclassified replies are touched.
- **Matview refresh hangs** — run `REFRESH MATERIALIZED VIEW CONCURRENTLY lead_status_mv;` directly in Supabase SQL editor.
- **NocoDB not seeing new columns after schema change** — NocoDB's "Sync Now" only detects table adds/drops, not column adds inside an existing view. Either delete the table from NocoDB and re-add via sync, or drop+recreate the view on the DB side to force re-detect.

## Cost / rate limits

- Instantly v2 API: ~100 req/min — script self-throttles via `RateLimiter` and backs off on 429.
- Haiku classification: ~$0.20 per 1,000 replies. Full backfill of 100k replies ≈ $20.
- Sync runs ~5–10 min for a 7-day window. Refresh-status ~30 min (paginates 80K leads).

## Reference docs

- `plan.md` — original spec
- `plan_v3.md` — multi-status columns + Instantly status refresh design
- `CLAUDE.md` — instructions for Claude Code agents working in this repo
