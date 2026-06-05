# Lead Scrape Automation — Operator Guide

End-to-end ownership of the "Jam submits a request → leads land in the 200k
pool" flow. Implementation plan lives in
`C:\Users\SH\.claude\plans\wise-pondering-pelican.md`; mockups in
`LEAD_AUTOMATION_MOCKUPS.html`.

## Pieces

| File | Role |
|---|---|
| `scrape_requests` table (Supabase) | Job queue + state machine. One row per request. |
| `prospeo_new_leads.scrape_request_id` (added column) | Tags each scraped row with the request that produced it. Used by the move-to-200k step. |
| `worker.py` | Long-running process. Polls every 60s, drives state transitions. |
| `notifier.py` | Thin Resend wrapper. Sends the "batch ready" email. |
| `Dockerfile.worker` + `railway.worker.json` | Railway service definition. |
| NocoDB views (configured manually, once) | Where Jam submits + approves. |

State machine:

```
  pending ──(worker scrapes)──► running ──► ready ──(all leads decided)──► moved
                                  │
                                  └─(error)──► failed
```

`status` stays `ready` while Jam reviews. Per-lead approval drives the move:

- Each row in `prospeo_new_leads` tagged with a `scrape_request_id` has its
  own `lead_approval` (`pending` / `approved` / `rejected`).
- BC-accepted leads start at `pending`; BC-auto-rejected leads (the
  `rejected=true` audit rows) are seeded `rejected`.
- Jam reviews each row in the per-batch NocoDB grid and toggles to `approved`
  or `rejected`. The worker keeps moving newly-approved leads into
  `lead_contacts` every poll (within ~60s) and stamps `lead_moved_at`.
- When no row is still `pending` AND every approved row has been moved,
  the worker auto-finalizes the request: `status='moved'`.

**Mass-approve shortcut.** `scrape_requests.approval` is also wired as a
batch-level convenience: when Jam sets the parent row's `approval='approved'`
in NocoDB, the worker flips every `lead_approval='pending'` lead for that
request to `'approved'` on the next poll, and the move/finalize chain takes
over on the same cycle. Gives her two paths in the same UI:
- "I trust BC's filter, take everything" → flip the parent row's approval.
- "I want to cherry-pick" → use the per-lead grid.

The leads Jam has already individually rejected are NOT overridden by the
mass-approve — only `lead_approval='pending'` rows get flipped.

---

## First-time deploy

### 1. Apply the schema (once, against production Supabase)

```bash
python scripts/apply_scrape_requests_schema.py
python scripts/apply_lead_approval_schema.py
```

Both idempotent — safe to re-run. The first creates the `scrape_requests`
table and adds `prospeo_new_leads.scrape_request_id`. The second adds
`prospeo_new_leads.lead_approval` + `lead_moved_at` for the per-lead
workflow (Flavor C).

### 2. Set up Resend (one-time, ~10 minutes)

1. Sign up at https://resend.com (free tier: 3,000 emails/month).
2. **Add a sending domain.** In Resend → Domains → Add `dwyer-enterprises.com`.
   Resend gives you 3 DNS records (SPF, DKIM, MX). Hand them to Anna or
   whoever manages the DNS. Wait for verification (~1–10 minutes).
3. Once verified, create an API key in Resend → API Keys. Save it as
   `RESEND_API_KEY` in your env vars.

If domain verification is blocked, the worker will still complete jobs and
mark them ready — emails just won't send. Operate fine without them while
DNS propagates.

### 3. Configure NocoDB views (one-time, ~20 minutes)

The schema is in Supabase but NocoDB caches metadata, so first:

1. Open NocoDB at https://vd-master-leads.up.railway.app
2. **Reload metadata** for the Supabase data source (Data Sources → ⋮ → Sync Now).

Then configure three views — two on `scrape_requests`, one on `prospeo_new_leads`:

**A. Submit form (on `scrape_requests`)** — what Jam fills out.
- Click `+` next to "Views" → "Form view" → name it **"Submit a request"**
- Show only: `requested_leads`, `industries`, `skip_industries`, `countries`, `notes`.
- Hide everything else.
- Make the form public (View Settings → "Public Form") and give the URL to Jam to bookmark.

**B. Track requests (on `scrape_requests`)** — Jam's overview.
- Default Grid view → rename to **"All requests"**. Sort by `created_at DESC`.
- Configure cell coloring on `status`: yellow for `ready`, green for `moved`, red for `failed`.

**C. Per-batch review grid (on `prospeo_new_leads`)** — where Jam reviews each lead.
- Click `+` next to "Views" → "Grid view" → name it **"Review batch"**.
- Filter: `scrape_request_id` IS NOT NULL AND `lead_approval` = 'pending'.
  - Optional: a second filter row Jam can edit to pin to one specific request
    id (or use Toolbar → "Search in view" by request id).
- Show these columns: `email`, `first_name`, `last_name`, `title`,
  `company_name`, `source_industry`, `lead_approval`, `lead_moved_at`,
  `scrape_request_id`.
- On the `lead_approval` column, change field type from Text to SingleSelect
  with options `pending`, `approved`, `rejected`. Default `pending`.
- Cell coloring on `lead_approval`: green for `approved`, red for `rejected`.

**Bulk-edit tip for Jam:** in NocoDB she can shift-click multiple rows and
bulk-edit `lead_approval` for the lot — useful when she trusts a whole
industry's results or wants to reject everything from one company.

### 4. Deploy the worker to Railway

```bash
# in the repo root
railway service create lead-scrape-worker
railway service link lead-scrape-worker
railway up --service lead-scrape-worker --dockerfile Dockerfile.worker
```

Or via the dashboard:
1. New service → "Deploy from Git" → pick this repo.
2. Settings → "Deployment" → Dockerfile Path: `Dockerfile.worker`.
3. Set environment variables (see next section).
4. Deploy.

### 5. Required environment variables

Set these on the Railway service (and locally in `.env` if testing):

| Var | Example | Notes |
|---|---|---|
| `SUPABASE_DB_PASSWORD` or `SUPABASE_DB_URL` | (from .env) | Same as existing services |
| `BETTERCONTACT_API_KEY` | (from .env) | Same as existing |
| `RESEND_API_KEY` | `re_abc123...` | From Resend dashboard |
| `NOTIFY_EMAIL` | `jam@dwyer-enterprises.com` | Where the "ready" email goes |
| `NOTIFY_FROM` | `Dwyer Lead Scraper <noreply@dwyer-enterprises.com>` | Optional; must be a verified sender on your Resend domain |
| `NOCODB_ROW_URL_TEMPLATE` | `https://vd-master-leads.up.railway.app/dashboard/#/nc/abc/def/?rowId={id}` | Expanded-row URL with literal `{id}` placeholder. To set it: open NocoDB → expand any row in `scrape_requests` → copy the URL bar → replace that row's id with the literal string `{id}`. Avoids guessing the NocoDB URL layout. |
| `NOCODB_BASE_URL` | `https://vd-master-leads.up.railway.app` | Optional fallback. Used as the email link only when `NOCODB_ROW_URL_TEMPLATE` is unset. |
| `WORKER_POLL_INTERVAL_S` | `60` (optional) | Defaults to 60 |

---

## Day-to-day operation

### Watching jobs

Railway → lead-scrape-worker → Logs. The worker prints one line per state
transition, e.g.:

```
[2026-06-04 14:30:01] worker starting (poll interval = 60s)
[2026-06-04 14:31:02] req #14: claimed, status -> running
[2026-06-04 14:31:02] req #14: scraping target=500, countries=['US','CA'], skip=['Food and Beverage Manufacturing']
[2026-06-04 14:48:33] req #14: scrape done, accepted=487, rejected=12
[2026-06-04 14:48:34] req #14: email sent
[2026-06-04 14:55:02] req #14: moved 22 approved lead(s) into lead_contacts
[2026-06-04 15:12:14] req #14: moved 105 approved lead(s) into lead_contacts
[2026-06-04 16:03:08] req #14: moved 360 approved lead(s) into lead_contacts
[2026-06-04 16:03:08] req #14: all leads decided — status=moved
```

(In the per-lead workflow each "moved N approved leads" line corresponds to
a batch of approvals Jam saved in NocoDB since the last poll.)

### What Jam sees

1. She fills the NocoDB form → row appears in "All requests" with `status=pending`.
2. Within ~60s the worker picks it up; `status` flips to `running`. The
   `scraped_count` field updates as the BC scraper runs (visible on each
   refresh).
3. When complete, `status=ready` and she gets an email with a link.
4. She opens the **"Review batch"** grid view on `prospeo_new_leads`,
   filters to this request id, and toggles each row's `lead_approval`
   to `approved` or `rejected`. She can do this in batches across multiple
   sessions — the worker keeps moving approved leads as they appear.
5. Once every row has a decision and every approved lead has been moved,
   `status='moved'` automatically. She doesn't have to manually close the
   batch.

---

## Troubleshooting

### A job is stuck in `running`

Cause: worker crashed mid-scrape (OOM, redeploy, lost connection).

The worker auto-recovers stuck jobs on startup: any row in `running` with
`started_at < now() - 1 hour` is demoted back to `pending`. Manual fix if
needed:

```sql
update scrape_requests
   set status = 'pending', started_at = null
 where status = 'running' and id = <id>;
```

### A job failed

Check `error_message` on the row. Common causes:

- **`InsufficientCreditsError`** — BC credits exhausted. Top up BetterContact,
  set the row back to `pending`, worker will retry.
- **`move-to-lead_contacts failed`** — `lead_contacts` schema drifted (e.g.
  a new NOT NULL column). Fix the INSERT in `worker.py::move_request_to_contacts`,
  redeploy, then either set the row back to `ready` for retry or move manually.

### Email isn't arriving

1. Check the Resend dashboard → Logs. Bounced? Domain not verified?
2. `email_sent_at` IS NULL but `status='ready'` → Resend rejected the send.
   The job still completed — Jam just needs to be told to check NocoDB
   manually.
3. If Resend is down or you've hit the free-tier cap, the worker keeps
   running. No fix needed beyond letting it expire / switching providers.

### Want to switch email providers later

`notifier.py` has a single function `send_batch_ready_email(req)`. Replace
the Resend HTTP call with SMTP, SendGrid, Postmark, etc. — no other file
changes needed.

### Want to run more than one worker

Both transitions use `FOR UPDATE SKIP LOCKED` on their SELECTs, so multiple
workers will never grab the same row. Spin up a second Railway service from
the same image — they'll cooperatively drain the queue.

---

## Rollback

If you need to disable automation entirely:

1. Stop the Railway worker service (Railway → service → Settings → Pause).
2. Optional: hide the NocoDB form view from Jam.
3. The `scrape_requests` table and `scrape_request_id` column are
   non-destructive — they just sit unused. No migration to roll back.

To remove entirely:

```sql
alter table prospeo_new_leads drop column if exists scrape_request_id;
drop table if exists scrape_requests;
```

`bettercontact_sync.py` still works as before — the new kwarg is optional.

---

## What this doesn't do (deferred)

- No editing a request after submission (Jam can submit a new one).
- No automatic load into Instantly — leads land in `lead_contacts`; the
  existing flow takes over.
- No multi-workspace `sent_messages` sync (separate ticket — see
  `FOLLOWUP_ANALYSIS_PLAN.md` §6).
