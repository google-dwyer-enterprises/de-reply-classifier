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
  pending ──(worker scrapes)──► running ──► ready ──(Jam approves)──► moved
                                  │            │                       
                                  │            └──(Jam rejects)─► rejected
                                  └─(error)──► failed
```

`approval` is a separate field (pending/approved/rejected) that Jam edits
in NocoDB. The worker only acts on `status='ready' AND approval='approved'`.

---

## First-time deploy

### 1. Apply the schema (once, against production Supabase)

```bash
python scripts/apply_scrape_requests_schema.py
```

Idempotent — safe to re-run. Creates the `scrape_requests` table and adds
`prospeo_new_leads.scrape_request_id`.

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

### 3. Configure NocoDB views (one-time, ~15 minutes)

The `scrape_requests` table now exists in Supabase but NocoDB caches
metadata, so:

1. Open NocoDB at https://vd-master-leads.up.railway.app
2. **Reload metadata** for the Supabase data source (Data Sources → ⋮ → Sync Now).
3. Open the `scrape_requests` table.
4. **Form view for Jam:**
   - Click `+` next to "Views" → "Form view" → name it **"Submit a request"**
   - Show only these fields: `requested_leads`, `industries`, `skip_industries`,
     `countries`, `notes`. Hide everything else.
   - Make the form public (View Settings → "Public Form") and give the URL
     to Jam to bookmark.
5. **Grid view for tracking:**
   - Default Grid view → rename to **"All requests"**. Sort by `created_at DESC`.
   - Configure cell coloring on `status`: yellow for `ready`, green for
     `moved`, red for `failed`.
6. **Approval dropdown:**
   - On the `approval` column, change the field type from Text to
     SingleSelect with options `pending`, `approved`, `rejected`.

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
[2026-06-04 14:52:15] req #14: approved, moving into lead_contacts
[2026-06-04 14:52:16] req #14: moved 487 rows -> status=moved
```

### What Jam sees

1. She fills the NocoDB form → row appears in "All requests" with `status=pending`.
2. Within ~60s the worker picks it up; `status` flips to `running`. The
   `scraped_count` field updates as the BC scraper runs (visible on each
   refresh).
3. When complete, `status=ready` and she gets an email.
4. She opens the row in NocoDB, sees a preview, sets `approval=approved`.
5. Within ~60s the worker moves the leads; `status=moved`, `moved_count` is set.

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
- No partial approval — whole batch or none.
- No automatic load into Instantly — leads land in `lead_contacts`; the
  existing flow takes over.
- No multi-workspace `sent_messages` sync (separate ticket — see
  `FOLLOWUP_ANALYSIS_PLAN.md` §6).
