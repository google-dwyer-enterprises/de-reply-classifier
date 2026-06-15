# Adding a NocoDB view

A runbook for surfacing new data to the client in NocoDB. Captures the
conventions and the things that have bitten before, so the next view is a
copy-paste away rather than a half-day of debugging.

NocoDB reads Postgres (Supabase). The client sees **views** — never the raw
tables. There are two shapes; pick the cheaper one that works.

---

## 1. Pick the shape

| Shape | When | Cost | Refresh |
|---|---|---|---|
| **Plain view** (default) | Query runs in well under a second on live data | Recomputed on every read | None — always live |
| **Materialized view + plain-view wrapper** | Query is expensive (multi-table joins over 100k+ rows, fuzzy matches, lateral subqueries) | Stored; read is instant | Must `refresh` explicitly |

**Default to a plain view.** The follow-up analysis views
(`followup_patterns_mv`, `followup_timing_mv`) are plain views despite the
`_mv` suffix — they aggregate ~3.7k rows in ~0.4s, so materializing buys
nothing and avoids the refresh dance. Reserve a real materialized view for the
genuinely expensive case (`lead_status_mv` joins `lead_contacts` +
`smartscout_brands` + fuzzy match across the whole lead base).

> Naming note: the `_mv` suffix on `followup_patterns_mv` is historical — it is
> a *plain* view. Don't let the name fool you into `refresh`-ing it (that
> errors: "is not a materialized view"). New plain views need no suffix.

---

## 2. Conventions (both shapes)

- **Title-Case, double-quoted aliases** for every client-facing column:
  `lc.company_name AS "Company Name"`, `l.reason AS "More detail about status"`.
  This is what the client reads in NocoDB; match the existing casing/spacing.
- **Postgres-owned objects.** Create the view as the connection role used by
  `db.connect()` (postgres). Postgres-owned views inherit grants, so you
  usually don't need to re-grant a *plain* view. (A materialized view wrapped
  by a plain view is the exception — see §4.)
- **Descriptive, not authoritative-looking, when the data is associational.**
  If a column is a lift/rate/association, name it so (and put the caveats in the
  accompanying HTML report, not the column name).
- **One script per view family** under `scripts/`, idempotent
  (`drop ... if exists` then `create`), runnable standalone. Mirror
  `scripts/apply_followup_patterns_view.py` (plain) or
  `debug/_mv_view_swap2.py` (MV swap).

---

## 3. Plain view — the easy path

```python
# scripts/apply_<name>_view.py  (mirror scripts/apply_followup_patterns_view.py)
from db import connect

VIEW = """
drop view if exists my_new_view;
create view my_new_view as
select
  l.lead_email,
  l.company_name as "Company Name",
  count(*)       as "Reply Count"
from ...
group by ...;
"""

conn = connect(); conn.autocommit = True
conn.cursor().execute(VIEW)
```

Run it, then do the **NocoDB registration** step (§5). That's it — the view is
live and recomputes on every read. No refresh, no cron.

---

## 4. Materialized view + wrapper — the `lead_status_mv` pattern

`lead_status_mv` is expensive, so it's materialized. NocoDB/PostgREST actually
read a **plain view `lead_status` that wraps the MV** (so the MV can be swapped
without NocoDB losing the object). This wrapping is why DDL changes are fiddly.

**The MV definition is NOT in `migrations.sql`.** Fetch the live definition
first:

```sql
select definition from pg_matviews where matviewname = 'lead_status_mv';
```

### Changing the MV (add/rename/retype a column)

- **Rename a column only:** `alter materialized view lead_status_mv rename column ...`
  works directly — no recreate.
- **Anything else (add column, change type, change the query):** full
  drop + recreate, in this order (worked example: `debug/_mv_view_swap2.py`):
  1. `drop view lead_status;`            ← the wrapper, first
  2. `drop materialized view lead_status_mv;`
  3. `create materialized view lead_status_mv as <new definition>;`  ← add the new column **here**
  4. `create unique index lead_status_mv_lead_email_idx on lead_status_mv (lead_email);`
     ← **required** for `refresh ... concurrently`; the refresh fails without it
  5. `create view lead_status as select ..., "New Column", lead_email from lead_status_mv;`
     ← add the new column **here too** (the wrapper enumerates columns)
  6. `grant select on lead_status to anon, authenticated, service_role;`
     ← re-grant the wrapper (a fresh view does not inherit the old grants)

### Refreshing the data (no DDL)

`leads_status_update.py` calls `db.refresh_lead_status()` at the end of
`run.py update-status`. Pure data refreshes need **no** NocoDB sync.

---

## 5. Register the view in NocoDB

NocoDB caches schema metadata, so a **new view or any DDL change** is invisible
until you tell NocoDB to re-read the schema:

1. In NocoDB, open the Postgres data source.
2. **Sync Now** (or disconnect + reconnect the data source).
3. The new view/columns appear; add it as a table/view in the relevant base.

**Pure data changes don't need this** — refreshing an MV or upserting rows shows
up on the next read automatically. Only *schema* changes (new view, new/renamed
column, type change) require Sync Now.

---

## 6. Gotchas that have cost time

- **`round(double precision, integer) does not exist`.** Postgres only has
  two-arg `round` for `numeric`. Cast first: `round(x::numeric, 1)`.
- **`information_schema.columns` does not list materialized-view columns** in
  this Postgres version. Inspect with `pg_attribute`:
  ```sql
  select attname from pg_attribute
  where attrelid = 'public.lead_status_mv'::regclass and attnum > 0 and not attisdropped;
  ```
- **Stale `idle in transaction` sessions lock tables indefinitely.** Before any
  DDL, check `pg_stat_activity` and `pg_terminate_backend()` anything
  idle-in-transaction older than an hour (27-day-old SSL-dropped zombies have
  blocked `ALTER TABLE` before).
- **Correlated lateral subqueries in an aggregation can blow up to a statement
  timeout** (O(n²)). Pre-aggregate in grouped CTEs instead — see the
  `percell → cli → agg` rewrite in `scripts/apply_followup_patterns_view.py`.
- **Refresh fails without the unique index.** `refresh materialized view
  concurrently` needs a unique index on the MV; recreate it after any MV
  recreate.
- **A fresh wrapper view loses grants.** Re-`grant select` to
  `anon, authenticated, service_role` after recreating the `lead_status` view.

---

## 7. Checklist

- [ ] Chose the shape (plain view unless the query is genuinely expensive)
- [ ] Title-Case double-quoted aliases on every client column
- [ ] Idempotent `scripts/apply_<name>_view.py`, runs standalone
- [ ] (MV only) unique index recreated; wrapper view recreated **and** re-granted
- [ ] Ran the script; verified row count + columns via `pg_attribute`
- [ ] **NocoDB Sync Now** (only needed for schema changes)
- [ ] If associational, the caveats live in an accompanying HTML report
