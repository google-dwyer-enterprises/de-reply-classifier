# Amazon Revenue QA Bot — Build Instructions

**Owner:** Hassan · **Rough-draft deadline:** **2026-07-01** (one week from the 2026-06-24 AI meeting)
**Source:** DE AI Cold Email Team Update (2026-06-24). Background/decision context: `AMAZON_REVENUE_BOT_PLAN.md`.
**Status:** spec — not started. Keep this doc LOCAL until Hassan says otherwise.

---

## 1. Why we're building this

The cold-email department is getting **garbage leads** — Victor estimates **50–70% of BetterContact leads fall outside ICP**. The automated lead system (Prospeo/BetterContact → brand-verify → review → DB) is good, but it does **not** confirm two things that actually matter for an Amazon marketplace-expansion offer:

1. **Is the brand actually selling on Amazon?**
2. **How much revenue are they doing on Amazon?** (keep only brands above a revenue floor)

This bot is a **QA / filtering layer** — the Amazon equivalent of the existing e-commerce/reseller QA (`brand_verify.py`). It sits *before* email extraction so we don't spend enrichment/verification credits on brands we'll throw away.

## 2. The agreed pipeline (from Victor's whiteboard + transcript)

```
[1] Interface: "how many leads do you want?"  (Jam)         ← already exists (submit form)
        │
[2] Analysis layer: use BEST categories + BEST titles       ← wire in category/title priority
        │   (from the live category-booking analysis)
        ▼
[3] BetterContact: find brands/leads in those categories    ← already exists
        │
[4] E-commerce QA bot: is it even an e-commerce company?     ← ALREADY DONE (brand_verify.py)
        │   (website-based; Victor: "I think we have this done")
        ▼
[5] ===== AMAZON REVENUE QA BOT (THE NEW BUILD) =====
        │   a. take the COMPANY NAME (do NOT grab email yet)
        │   b. search the brand on Amazon → is the brand present?
        │   c. Helium 10 (UI / Chrome extension) → sum monthly revenue
        │      across all listings that belong to this brand
        │   d. brand-match logic: resolve name variations to ONE entity
        │      (e.g. "Anchor" vs "Anchor Electronics Inc" vs the Amazon storefront)
        │   e. keep ONLY if revenue ≥ configurable threshold (e.g. $500k/yr)
        ▼
[6] Grab email address  (only for brands that passed [5])
        │
[7] Output: revenue as its OWN column/field in the dataset
        ▼
[8] Approval flow:  bot spits out + notifies
        → Jam approves → Million Verifier → final approval → into the 200k DB
```

Steps **1–4 and 6–8 largely exist** in the current system. The **net-new work is step 5** (Amazon presence + Helium 10 revenue + brand-name matching) plus wiring 2 (category/title priority) and 7 (revenue column).

## 3. Detailed requirements for the new build (step 5)

1. **Amazon presence check** — for each brand, confirm it actually sells on Amazon (search the brand; confirm via storefront / "Sold by <brand>" on listings). Our existing `amazon_presence` (SmartScout-registry match) only covers ~42% — this needs a live check.
2. **Revenue estimate via Helium 10 — UI/Chrome-extension, NOT the API.** The Helium 10 API is **$1,500/mo — too expensive** (decision: do not use it). Use the Helium 10 Chrome extension on the Amazon search results and **sum the monthly revenue** of all listings belonging to the brand. Convert to yearly for the threshold.
3. **Configurable revenue threshold** — default ~**$500k/yr**; must be an adjustable minimum (Jam/Victor can change it).
4. **Brand-matching logic (the hard part)** — variations of the same brand must resolve to one entity. The company name from BetterContact won't always equal the Amazon storefront name ("Anchor" vs "Anchor Electronics Incorporated"). **AI is expected here** to decide whether the BetterContact company and the Amazon brand are the same. Don't false-merge unrelated brands (the "Joe Schmo" case — no real Amazon match → drop).
5. **Revenue as a separate output field/column** in the final dataset.
6. **Order of operations matters:** Amazon presence + revenue check happens **before** grabbing the email (saves credits).

## 4. Hard constraints / known pain points

- **Amazon reCAPTCHA** — DE has built this bot before and Amazon's reCAPTCHAs were the killer; they could not get past them reliably. Expect this. Mitigations to consider: a real browser session/profile, residential proxies, human-in-the-loop CAPTCHA solve, pacing, or a managed Amazon-data API as fallback. Hamza previously researched revenue-data platforms and the team landed on Helium 10.
- **Helium 10 account** — Victor/Anna are setting up the subscription (affiliate sign-up in progress as of the meeting). Build assuming UI/extension access, not API.
- **Accuracy is the point** — the whole reason for this layer is *accurate* revenue numbers; prefer correctness over speed.

## 5. Acceptance criteria for the 2026-07-01 rough draft

A rough draft that demonstrates the **core loop on a small sample**:
- Given a brand/company name → search Amazon → determine presence (yes/no).
- For present brands → pull Helium 10 monthly revenue via the extension → sum per brand → yearly.
- Apply a configurable threshold → keep/drop.
- Emit revenue as a field.
- (Approval flow + DB insert can reuse the existing worker path; doesn't need to be polished for the draft.)

It does **not** need full CAPTCHA-proof scale by Jul 1 — it needs to prove the loop works end-to-end on a handful of brands and surface the real blockers (CAPTCHA, brand-match edge cases).

## 6. Open questions to resolve

- Helium 10 subscription confirmed + extension login available? (Anna/Victor)
- CAPTCHA strategy for production scale — proxies vs managed API fallback?
- Exact revenue threshold + is it yearly or monthly in the UI input?
- Where the revenue column lives (`prospeo_new_leads` / `lead_contacts`) and how it surfaces to Jam's review + the 200k DB.
- Does "Amazon presence" require a brand storefront, or is any "Sold by <brand>" listing enough?

## 7. Related existing building blocks (reuse, don't rebuild)

- `brand_verify.py` — the e-commerce/reseller QA funnel (step 4) + `amazon_presence` (SmartScout registry).
- `smartscout_brands` / `lead_smartscout_match` — brand → market-data lookup (revenue cross-check / fallback).
- `bettercontact_sync.py` — lead sourcing (step 3).
- `millionverifier.py` — email-verification gate (step 8), already runs after Jam's approval.
- The submit form + worker + `/batches` review (steps 1, 8).

---

## 8. Findings & Decisions Log (keep this current as the feature develops)

**D1 — Revenue floor = $500,000/yr** (configurable). Confirmed by Hassan 2026-06-30; no further sign-off needed.

**D2 — Playwright does NOT bypass CAPTCHAs.** Tested live (Playwright → `amazon.com/s?k=OLAPLEX`): one search returned real results ("1-48 of 187 results"), but that's luck per-request — at scale Amazon will CAPTCHA, which is what burned the team before. Also: the automated browser defaulted to **Pakistan/PKR geo** → must pin to **US** for US marketplace/Helium-10 data.

**D3 — Presence/scraping route: managed API > DIY Playwright** (cost + reliability, at ~10k brand-checks/mo, *ballpark — verify*):
  - *Managed API* (Rainforest/Bright Data/Oxylabs/Apify): ~$25–75/mo, near-zero maintenance, high reliability.
  - *DIY Playwright + residential proxies + CAPTCHA solver*: ~$100–290/mo (proxies dominate) **+ heavy maintenance + fragile** (Amazon changes defenses; team already failed once).
  - **Decision:** use a managed API for the live presence/"Sold by" check; do NOT build raw-Amazon scraping ourselves.

**D4 — SmartScout & Helium 10 are BOTH BSR-based estimates** from public Amazon data (Best Sellers Rank → units × price, aggregated to brand). Neither is ground truth (~±30–50% on an individual brand). For a **coarse $500k/yr keep/drop**, SmartScout (already in our DB, free) is good enough; Helium 10's edge is **freshness + coverage**, not method. → Re-evaluate paying for Helium 10 once SmartScout's coverage/staleness is shown insufficient.

**D5 — Revenue = cascade, cheap-first:** cache → SmartScout → Helium 10 (only for SmartScout misses **and** borderline values near the floor). Caches every result in `brand_revenue_cache`. `unknown` ≠ `drop` — route to **Jam review**. Helium 10 = flat **subscription** via the Chrome extension (~$39–279/mo plan), **not** the $1,500/mo API.

**D6 — The real accuracy bottleneck is BRAND MATCHING, not the revenue source.** The existing `lead_smartscout_match` (`token_set_ratio`) scored 100 on word-containment — "Blue Apple Co.", "The Good Apple", even "NAF NAF" → brand **"apple" ($2.9B/yr)**, which would wrongly PASS. Fixed with a **strict matcher** (`amazon_brand_match.py`): exact-normalized → `token_sort_ratio` (penalizes extra tokens) + length guard → Haiku grey-zone verify. Verified: the "apple" false-positives are gone.

**D7 — Helium 10 access = web tools + persistent logged-in profile** (chosen 2026-06-30).
  - *Surface:* Helium 10's **web tools** (query H10's own site), NOT the Xray-on-Amazon extension → avoids Amazon CAPTCHA/geo/proxies entirely. Same BSR estimates either way; web-tools just doesn't touch Amazon pages.
  - *Session:* a **persistent logged-in browser profile** (log in once with the existing **google@dwyer-enterprises.com** H10 account; the bot reuses it) — NOT exported cookies (those expire and H10's SPA stores its token in localStorage, so cookie-only injection is fragile/can fail).
  - *Account:* confirmed registered under google@DE.
  - *Implementation:* `helium10_revenue.py` — connection logic done (persistent-context via `H10_USER_DATA_DIR`, or connect-over-CDP via `H10_CDP_URL`); fails safe to None (→ REVIEW) until configured.
  - **To finalize (needs the live logged-in account):** (1) stand up the profile/VM logged into H10; (2) confirm the exact tool URL (`H10_TOOL_URL`, default Black Box) + the two selectors `H10_BRAND_SELECTOR` and `H10_REVENUE_SELECTOR` (run with `H10_DEBUG=1` to dump the page and read them off). Until those are set, it stays None-safe.
  - *Note:* automating H10's UI is subject to their usage limits/ToS — keep volume modest + cache (we do).

**D8 — Deep-research verdict (2026-06-30): the Helium 10 UI-automation route is buildable but NOT recommended.** Cited, adversarially-verified research (run `wf_8b874e7e`; primary sources = Helium 10 ToS/KB/pricing, Google support, Playwright docs):
  - **ToS breach / ban risk:** Helium 10 ToS §2(C) explicitly prohibits "access… through any automated means (including use of scripts or web crawlers)"; §4 reserves the right to suspend/terminate **without notice**. Automating its UI puts the account at risk.
  - **Google blocks automated logins:** Google stops sign-ins "controlled through software automation" + anti-bot (CAPTCHA/ban). You **cannot script the login**; only a **one-time MANUAL human login (e.g. via VNC) + persisted session** works (confirmed pattern).
  - **Not truly unattended:** Playwright has **no auto-reauth** — sessions expire (~24h typical; server-side expiry uncontrollable) and need **periodic manual re-login via VNC**. ("Subsequent runs need no re-auth" was REFUTED 0-3.)
  - **Capability exists but accuracy unverified:** Black Box's *Exact Brand/Seller Search* enumerates a brand's products (Platinum tier — **$129/mo monthly, $99/mo annual**; *not* the older ~$39–99 figures, which are stale). But summing per-product → brand revenue is an inference, and **no source verified the revenue is accurate enough to gate on**.
  - **Black Box freshness (days stale) = UNVERIFIED** — earlier "days to ~2 weeks" was an assumption, not confirmed. Read the "data as of" date in the live tool.
  - **REVISED RECOMMENDATION:** for a coarse $500k gate at modest volume, **stay on SmartScout** (already integrated, zero ToS/automation risk; just refresh the export for freshness). Optionally add a **managed Amazon-data API (e.g. Rainforest)** for presence/gap revenue *after* confirming it returns revenue estimates. **Shelve the Helium 10 UI automation** — ToS + ban + manual-reauth + unverified accuracy make it a poor fit for a coarse gate. (Supersedes the Helium-10-fallback emphasis in D5/D7; the cascade keeps the H10 hook but defaults to SmartScout-only.)

**Open questions (still need answers):**
- Helium 10 account + a US-pinned browser/server — ready, or still being set up?
- Generic single-word company names ("Studio") can exact-match a generic SmartScout brand — add a generic-token guard.
- Where the revenue column lives (`prospeo_new_leads`) + how it surfaces to Jam + the 200k DB.
- Managed-API provider choice + exact current pricing (run a research pass before committing).

## 9. Build status (rough draft — 2026-06-30)

| Component | File | Status |
|---|---|---|
| Strict brand matcher (exact → guarded fuzzy → LLM) | `amazon_brand_match.py` | ✅ built, kills the false positives |
| Revenue cascade + $500k floor + KEEP/DROP/REVIEW verdicts | `amazon_revenue_qa.py` | ✅ built, runs on real data (`--demo`) |
| SmartScout revenue provider | `amazon_revenue_qa.smartscout_revenue` | ✅ |
| Revenue cache | `brand_revenue_cache` table | ✅ created |
| Helium 10 provider (web tools + persistent profile) | `helium10_revenue.py` | ⏸ **shelved per D8** — ToS breach + ban risk + manual-reauth + unverified accuracy. Code kept behind the cascade hook if ever revisited; default = SmartScout-only. |
| Revenue source (revised direction) | SmartScout (+ optional managed API) | → **SmartScout is the route** for the coarse gate; refresh the export for freshness. Evaluate Rainforest only after confirming it returns revenue (open Q2). Full research: `HELIUM10_FEASIBILITY_RESEARCH.md`. |
| Amazon presence check | `amazon_presence.py` | 🟢 **logic validated live** (Playwright, free — "nanobebe"→ON AMAZON, "Digital Sports Mgmt"→not a brand). Managed-API (Rainforest) provider coded + fail-safe; needs `RAINFOREST_API_KEY` (free trial) for production scale. This is the higher-leverage piece — it recovers real Amazon brands SmartScout misses (~half the 80%) and flags the rest as out-of-ICP. |
| Wire into pipeline (after e-commerce QA, before email) + Jam review surfacing | — | ⏳ next |

**Run the draft:** `python amazon_revenue_qa.py --demo [--llm]`. All files are LOCAL/uncommitted per the standing instruction — do not commit until Hassan says so.
