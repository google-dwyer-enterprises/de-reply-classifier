# Production-Readiness Remediation Plan

Derived from the 2026-07-08 three-part production-readiness audit (scraping
reliability, data-integrity/QA correctness, ops/infra). The scraping
pipeline's core engineering is production-grade (human-in-the-loop backstop,
conservative credit accounting, resumable per-page checkpoints, fail-to-REVIEW
QA gates). This plan closes the remaining "fix soon" and "hardening" gaps.

Status legend: **[DONE]** shipped · **[TODO]** planned.

---

## Phase 0 — 🔴 blockers (do first; some are provisioning, not code)

- **[TODO] Credit plans undersized.** Rainforest Hobbyist (500/mo) and BetterContact
  (~small plan) can't sustain 20k accepted leads/month (~7k Rainforest + ~83k BC
  credits). Provisioning decision (Victor): upgrade Rainforest → Starter and BC →
  a volume/Enterprise plan. No code change.
- **[TODO] Alerting reaches no one.** Cron service appears to lack
  `RESEND_API_KEY`/`NOTIFY_EMAIL`, and `notifier.py` has no resend.dev fallback
  (unlike `credit_alerts`/`job_monitor`). Fix: give `notifier.py` the same
  `_resolve_sender` fallback + `@`-validation; set Resend env on the cron service.
- **[TODO] MillionVerifier no-key silently moves UNVERIFIED leads** into
  `lead_contacts` (`worker.py:589`). Fix: when a key was expected but the gate is
  disabled, hold leads unmoved (or fail loud) rather than move unverified.
- **[TODO] Prospeo `--with-mobile` has no credit cap** (`run.py:420`,
  `prospeo_sync.py:1316`). Fix: refuse `--with-mobile` without `--max-credits`
  (parity with the BC phones guard).
- **[TODO] LLM ICP gate fail-OPEN default** (`bettercontact_sync.py:1217`) —
  missing domain key defaults to "brand"/accept. Fix: default to
  "unknown"/reject (fail-closed).

---

## Phase A — quick config & safety wins

- **[DONE] A1 — `AMAZON_QA_ENFORCE` env-driven** (`bettercontact_sync.py`). Now
  `os.environ`-toggleable (default OFF/shadow); flip to enforce via a worker env
  var, no code change.
- **[DONE] A2 — healthcheck + gunicorn timeout.** `railway.web.json` →
  `healthcheckPath: /healthz`; `Dockerfile.web` gunicorn → `--timeout 120
  --graceful-timeout 30` (was default 30s vs 300s DB timeout → analytics 502s).
- **[DONE] A4 — hardening bundle.** Session cookie `Secure`+`SameSite`+lifetime
  (`app.py`); over-annualization guard now also flags the **0-ratings** case →
  REVIEW (`amazon_revenue_qa.py`, +test); removed the `if moved or True:`
  dead condition (`worker.py`).
- **[TODO] A3 — pin dependencies + unify Python.** `requirements.txt` all `>=`
  (no lockfile); cron on 3.11 vs web/worker on 3.12. Pin `==` for the key libs;
  unify all Dockerfiles to 3.12-slim. Own PR — rebuild all three services + smoke
  test, because a pin can surface an incompatibility.

---

## Phase B — worker robustness (higher care; touches worker.py)

- **[TODO] B1 — stranded leads on non-`ready` requests.**
  `find_requests_with_pending_moves` (`worker.py:490`) requires `r.status='ready'`,
  so approved-but-unmoved leads on a `failed`/`moved` request never move
  (manual `export-leads` only). Fix: (1) relax the status filter (the real gate is
  `lead_approval='approved' AND lead_moved_at IS NULL`; the move is already
  idempotent + row-locked); (2) add a SIGTERM graceful-shutdown handler so a
  worker redeploy finishes + commits the current unit instead of orphaning it.
- **[TODO] B2 — single-threaded worker stalls approvals during long scrapes.**
  The poll loop runs `process_one_pending_request` synchronously (`worker.py:864`);
  a multi-hour scrape blocks approve/move/finalize for already-ready batches.
  Fix (no threads): the scrape is per-page resumable — run the fast maintenance
  cycle between pages (maintenance callback), or bound pages-per-loop and return.
  Soak-test with a concurrent approval before trusting. (Rejected: background
  thread — psycopg2 conn not thread-safe.)

---

## Phase C — remaining minor hardening

- **[TODO] C1 — `brand_verify` domain-cache TTL.** `domain_brand_verdicts` reads
  (`brand_verify.py:175`) have no TTL (unlike the 90-day Amazon cache); a
  brand→reseller pivot is never re-caught. Add a `fetched_at` TTL read filter
  (add the column if absent).
- **[TODO / decision] C2 — batch share-link write auth.**
  `/batch/<token>/mass-approve|mass-reject|bulk-update` (`app.py:615`) are
  token-only yet write approvals. Decide: accept + document (NocoDB share-link
  model), or require a role on the mutation routes while keeping the view public.

---

## Sequencing, testing, rollout
- **Order:** Phase 0 → A → B1 → B2 → C. Phase A is safe to ship immediately;
  B2 wants a live soak.
- **PR grouping:** A1+A2+A4 (this PR), A3 (own PR), Phase-0 code fixes (own PR),
  B1, B2, C1 each own PR.
- **Per PR:** `python -m unittest discover -s tests` green; deploy per service
  (web auto-tracks `main`; worker + cron manual; never redeploy the worker while a
  `scrape_requests` row is `running`).
- **Rollback:** changes are additive/config; revert-and-redeploy is clean. A3 is
  the only real rebuild risk — keep the prior image tag.
- **Note:** this was a code/config review, not a load/soak test.
