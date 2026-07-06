# Amazon Revenue Scraping Bot — Research & Implementation Plan

**Status:** Research/plan only — nothing built, nothing committed (per Hassan, 2026-06-22).
**Owner:** TBD — Victor floated giving this to "Sayad"; confirm before building.

---

## 1. Goal (as stated)

For **every lead** (starting with the BetterContact lists), get its Amazon presence,
reseller status, and **estimated revenue** — because SmartScout only covers some brands.

Four outputs per lead:
1. **"Are they on Amazon?"** — yes/no (some brands aren't on Amazon at all → must detect that).
2. **Reseller check** — does the listing's seller/shipper match the brand name? If not → reseller.
3. **Estimated revenue** — via Helium 10, from the brand's **own organic listings only** (exclude sponsored, exclude other brands' products that appear in search).
4. **Write the revenue back** to a column (e.g. "Estimated Helium 10 Revenue").

---

## 2. What ALREADY exists (so we complement, not duplicate)

A full code map was done before this plan. Summary of the relevant current state:

| Need | Exists today? | How | Coverage / gap |
|---|---|---|---|
| "Are they on Amazon?" | ⚠️ Partial | `brand_verify._amazon_presence` → fuzzy match against the **SmartScout brand registry**; stored on `prospeo_new_leads.amazon_presence`, shown in the reviewer UI | **`"no"` really means "not in SmartScout," NOT "not on Amazon."** Misses any Amazon seller SmartScout doesn't have. |
| Revenue / market data | ⚠️ Partial | `smartscout_brands.estimated_monthly_revenue` (+ units, sellers, growth…), matched to leads by **normalized company name** (`smartscout_resolve.py`, fuzzy ≥92 / LLM 85–92), surfaced in `lead_status_mv` as "Estimated Monthly Revenue" | **Only ~42% of leads match a SmartScout brand.** The other **~58% have NULL revenue.** This is the core gap the bot fills. |
| Reseller check | ✅ Yes, but website-based | `brand_verify.py` bv3 funnel: Shopify `/products.json` vendor analysis → vendor-ownership web search → SmartScout confirm → homepage signals ("authorized dealer", shop-by-brand nav) | **No Amazon-listing-level seller check exists.** Today's reseller signal is the *website*, never the Amazon "Sold by / Ships from / Buy Box". This bot's seller-match is **net-new signal**. |
| Browser / marketplace scraping | ❌ None | All scraping today is API-based (BetterContact/Prospeo REST) + two HTTP probes (Shopify JSON, homepage). No Playwright/Selenium/Helium 10 anywhere. | The bot would be the **first true browser/marketplace scraper** in the codebase. |

**Data flow today:** BetterContact leads land in **`prospeo_new_leads`** (the "BetterContact lists") → on Jam's approval the worker copies them to **`lead_contacts`** → matched to SmartScout via **`lead_smartscout_match`** → surfaced in **`lead_status_mv`** (what the client sees). A new revenue column would sit alongside SmartScout's, keyed per-lead (most natural: a sibling table keyed by `lead_email`/domain, like `lead_smartscout_match`, OR a column on `lead_contacts`).

**Takeaway:** the bot is mainly about (a) **true Amazon presence** for the brands SmartScout misses, (b) a **listing-level seller/reseller** signal that doesn't exist yet, and (c) **revenue for the ~58%** SmartScout can't cover.

---

## 3. The hard part: how do we actually get the data?

Getting a competitor brand's Amazon revenue is the crux. There are three families of approaches. **Amazon SP-API is NOT an option** — it only exposes *your own* seller account, not competitors.

### Option A — Browser automation + Helium 10 Xray extension (the literal ask)
Drive a real (headful) Chrome with the **Helium 10 Xray** extension installed and a logged-in paid Helium 10 seat; search the brand, read the revenue numbers the extension overlays on the page.
- **Pros:** gives Helium 10's *exact* numbers (the figure Victor trusts) + Buy-Box / seller / # sellers in the same view.
- **Cons / requirements (significant):**
  - A server running **headful Chrome** (extensions don't load in normal headless) — needs a virtual display (xvfb) or a hosted browser.
  - A **paid Helium 10 account** + keeping its login session alive (re-auth, 2FA, session expiry).
  - **Amazon anti-bot is the real risk** — CAPTCHAs, IP throttling, "dogs of Amazon" blocks. At any volume this needs **residential/rotating proxies** + human-like pacing, and still breaks often.
  - **ToS exposure:** automating Amazon + automating a Helium 10 extension both push against their terms.
  - Brittle: a Helium 10 UI/DOM change or Amazon layout change breaks the scraper.
- **Verdict:** highest fidelity, **lowest robustness**, most ops overhead. This is exactly the "server + Amazon anti-bot + Helium 10" difficulty already flagged.

### Option B — Helium 10 official API
Helium 10 *does* have an API, but it's **Enterprise-plan only** (custom pricing, sales consultation). Xray-style metrics may be reachable there.
- **Pros:** clean, supported, no scraping.
- **Cons:** likely expensive; needs a sales call to even price; unclear it exposes the exact Xray revenue per arbitrary brand search. Worth a 30-min call to confirm before assuming.

### Option C — Managed Amazon-data API (Rainforest / Oxylabs / Easyparser / Keepa)
Third-party services that **handle the anti-bot/proxy problem for us** and return Amazon search results, product details, **seller offers ("Sold by" / "Ships from")**, BSR, price, ratings. We then **estimate revenue from BSR** (category-specific BSR→sales curves; Keepa specializes in this).
- **Pros:** robust at scale (they own the proxy/CAPTCHA fight), predictable per-request cost (~$49+/mo tiers), legal-ish (vendor takes the ToS risk), gives the **seller field directly** (solves the reseller check cleanly), no Helium 10 seat or headful-Chrome server.
- **Cons:** revenue is *our* estimate from BSR, **not Helium 10's exact number** (close, not identical); per-request cost scales with lead count; BSR→revenue accuracy varies by category.
- **Verdict:** most robust and lowest-maintenance; the realistic path for "every lead" at scale.

### Recommendation (for discussion)
A **hybrid, validated with a POC** is likely best:
- Use **Option C** (a managed API, e.g. Rainforest/Keepa) as the workhorse for **presence + seller/reseller + revenue estimate** across all leads — it's the only approach that survives at scale.
- Optionally keep **Option A (Helium 10 Xray)** as a **manual/spot-check tool** for high-value leads where Victor wants the exact Helium 10 figure (or run it semi-manually with Instant Data Scraper).
- **Do a POC first** (Section 6, Phase 0): run ~50 brands through both a managed API and Helium 10, compare against SmartScout where both exist, and let the accuracy/cost numbers decide. Don't commit to building the full headful-Chrome+Xray rig before seeing whether a managed API is "good enough" for Victor.

---

## 4. Per-requirement design

**Input:** the brand/company name + domain for each lead in `prospeo_new_leads` (and/or `lead_contacts`). We already store `company_name`, `company_domain`, `resolved_company_name`.

1. **Are they on Amazon? (true presence)**
   - Search Amazon for the brand; if ≥1 organic listing's **brand field** matches the company → `yes`, else `no`.
   - Edge cases: generic brand names (false positives), brand sells under a different name, brand present but only via resellers. Use the brand field on the listing + domain corroboration, not just the search string.

2. **Reseller check (listing-level — net new)**
   - For the brand's own listings, read **"Sold by"** (and "Ships from" / Buy-Box winner).
   - If the seller name ≈ the brand → first-party/brand-controlled. If it's Amazon.com or a 3P seller name that doesn't match → **reseller present**.
   - Edge cases: "Ships from Amazon, Sold by <Brand>" (FBA, still first-party); multiple offers (use Buy-Box seller); brand sells *and* resellers exist (flag "mixed").

3. **Estimated revenue (own organic products only)**
   - **Exclude sponsored** results (sponsored ≠ organic rank → inflates/misattributes).
   - **Only the brand's own products** — filter listings whose brand ≠ the company (other brands appear in any search).
   - Aggregate: define the figure — sum of the brand's top-N organic listings' estimated monthly revenue? (Helium 10 Xray sums what's on the page.) **Needs a definition decision** (Section 5).

4. **Write-back**
   - Store per-lead: `amazon_on (yes/no)`, `amazon_reseller (yes/no/mixed)`, `amazon_seller_name`, `helium10_estimated_revenue` (or `amazon_est_monthly_revenue`), `amazon_data_source`, `amazon_checked_at`.
   - **"Write back to BetterContact"** — BetterContact is an enrichment *API*, not a datastore we own columns in. This almost certainly means **our record of the BetterContact leads** (`prospeo_new_leads`, and/or `lead_contacts`), surfaced in `lead_status_mv` next to "Estimated Monthly Revenue" and in the reviewer UI. **Confirm with Reggie/Victor** (Section 7).

---

## 5. Key challenges & edge cases (must be handled)

- **Brand → Amazon match ambiguity:** generic/short names, brands selling under a different storefront name, common-word brands → false matches. Mitigate with brand-field match + domain/category corroboration; mark low-confidence.
- **Sponsored vs organic:** must reliably strip "Sponsored" tiles.
- **Other brands in results:** only keep listings whose brand matches; everything else is noise.
- **Revenue definition:** top product vs sum of own listings vs storefront total — pick one and document it (and note it won't equal SmartScout's brand-level number).
- **Not on Amazon:** clean `no` (distinct from "blocked"/"no data").
- **Anti-bot (Option A):** CAPTCHAs, IP bans, rate limits → proxies + pacing + retry/backoff; expect partial failures.
- **Helium 10 session/seat (Option A):** login expiry, 2FA, per-seat cost, ToS.
- **Scale & cost:** how many leads total? (count `prospeo_new_leads`/`lead_contacts`.) At managed-API per-request pricing, cost = leads × searches/lead × $/request — size it before committing.
- **Freshness:** revenue changes; decide refresh cadence (one-time vs periodic) and store `*_checked_at`.
- **Validation:** for leads where SmartScout *also* has revenue (~42%), compare the bot's number to SmartScout as a sanity/accuracy check.

---

## 6. Phased implementation plan

**Phase 0 — Approach decision (POC, ~1–2 days, no production code).**
Pick ~50 representative BetterContact brands (mix of SmartScout-covered and not). Run them through (a) a managed Amazon-data API and (b) Helium 10 Xray (manually or scripted). Capture presence, seller, and revenue. Compare to SmartScout ground truth where available. **Deliverable:** an accuracy/cost/robustness table → decide Option A vs C vs hybrid.

**Phase 1 — Pipeline on a small batch (after approach chosen).**
Build the resolver for the chosen source: brand → Amazon search → own organic listings → {presence, seller/reseller, revenue}. Run on a few hundred leads. Validate against SmartScout. Store results in a **new table keyed by domain/`lead_email`** (mirrors `lead_smartscout_match`), not yet surfaced.

**Phase 2 — Write-back & surfacing.**
Add the columns to `lead_status_mv` (next to "Estimated Monthly Revenue") + the reviewer UI; backfill the "Are they on Amazon (live)?" so `no` finally means "not on Amazon." Confirm the write-back target with Victor/Reggie first.

**Phase 3 — Scale, schedule, monitor.**
Run across all leads; add to the pipeline (or a separate scheduled job); monitor block/failure rates and cost; set a refresh cadence.

---

## 7. Open questions / clarifications needed before building

1. **Ownership** — is this Hassan's or Sayad's? (Victor floated Sayad.) Avoid double-building.
2. **"Write back to BetterContact"** — confirm this means our DB (`prospeo_new_leads`/`lead_contacts` → `lead_status_mv`), not literally pushing data into BetterContact.
3. **Must it be Helium 10's exact number**, or is a comparable BSR-based estimate (managed API) acceptable? This decides Option A vs C and the whole cost/robustness profile.
4. **Helium 10 account** — do we have a seat/plan? Enterprise API budget? Who owns it?
5. **Scale** — how many leads, and how fresh must revenue be? (drives cost.)
6. **Proxy/infra budget** for Option A (residential proxies + a headful-Chrome server) vs managed-API per-request budget for Option C.
7. **Accuracy bar** — what's "good enough" for Victor to act on?

---

## 8. Bottom line

The bot mainly fills three real gaps: **true Amazon presence** (vs SmartScout-only), a **listing-level seller/reseller signal** (net-new), and **revenue for the ~58%** SmartScout misses. The literal "browser + Helium 10 Xray" approach gives the exact numbers but is the most fragile and ops-heavy (Amazon anti-bot + extension automation + a paid seat + proxies). A **managed Amazon-data API** is far more robust at scale and returns the seller field directly, at the cost of revenue being a BSR-based estimate rather than Helium 10's exact figure. **Recommend a short POC comparing both before committing to a build** — and resolve the open questions (especially ownership and whether the exact Helium 10 number is required) first.

---

## 9. Anna's gated plan + the match test

> **⚠️ CORRECTED 2026-06-25 — these are the final numbers.** The 2026-06-23 run below was computed against the **stale May-8 `smartscout_brands` DB copy (275,122 brands)**. Re-run against the **full combined file `exports/Smartscout Brands Combined.xlsx` (513,760 brands)** with Anna's **US filter + $500K/yr**: **62.3% overlap (16,022 of 25,701 US brands), net-new = 9,679 (~9.7k).** Key catch: the broader ≥$40k/mo set is **53% Chinese sellers** (54,530 CN vs 39,720 US) — Anna's US filter excludes those; the all-country net-new was a misleading ~52k. **Decision unchanged: ~9.7k net-new is far under 50k → don't build the SmartScout reverse-lookup bot → Helium 10 enrichment.** Done in-memory, no DB upload. The 06-23 figures are kept below for the audit trail.

### (2026-06-23 run — superseded, on the stale DB copy)

Anna reframed this so the build decision is **data-gated**, not debated. Step 1 (owner: Hassan, due before the Wed AI call) → the number then auto-selects the build:

- **If ≥70% of the SmartScout list is already in our DB** → skip the SmartScout bot (we'd only be refreshing numbers) → build the **Helium 10 enrichment bot** instead.
- **If 50k+ brands are net-new** → build the **SmartScout reverse-lookup bot** (company name → Google → ecomm domain → BetterContact → domains/emails).
- Anna's heads-ups: SmartScout gives **company name only, no domain** (so BetterContact hit-rate on that route is low); and **the Helium 10 build goes to the outside AI expert, not Hassan.**

### Match test — results
SmartScout has **no country field** (it's Amazon-US data, so "US filter" = the whole 275,122-brand table). Matched on **normalized brand name** (exact) against our universe of **~180,020 distinct companies** (lead_contacts + prospeo_new_leads):

| Revenue threshold | SmartScout brands | Already have | Overlap % | Net-new |
|---|---|---|---|---|
| **$500K/yr (≥$40k/mo)** | 37,103 | 14,897 | **40.2%** | **22,206** |
| $1M/yr (≥$80k/mo) | 25,111 | 11,311 | 45.0% | 13,800 |
| $250K/yr (≥$20k/mo) | 53,070 | 18,524 | 34.9% | 34,546 |

**Read:** the result lands **between** Anna's two thresholds — overlap ~40% (not ≥70%) and net-new ~22k (not ≥50k) at the $500K floor. So the decision is **not automatic**; it's a judgment call on whether ~22k new brands justifies the reverse-lookup build. ($500K/yr used because it matches our existing pipeline floor — confirm the intended threshold.)

### Why these numbers are not yet decision-grade (fuzzy matching)
The match is **exact normalized-name** → a **lower bound on overlap**. The same brand appears under different strings across sources (Amazon brand name vs legal/company name; `Inc/LLC/Co/Ltd/The/&` noise; abbreviations/DBAs), which exact match misses. A **fuzzy pass** (token-ratio ≥ ~92, the technique `smartscout_resolve` already uses, with length-ratio/retailer-vocab guards + first-token blocking to stay tractable) would raise the true overlap and lower net-new — and since the decision hinges on the 70%/50k lines, the fuzzy number is the one to bring to the call.

### Decision-grade update (fuzzy match + revenue bands), 2026-06-23
- **Fuzzy overlap (token-ratio ≥92, first-3-char blocking)** at $500K/yr: **55.5%** (20,586 of 37,103) — up from 40.2% exact; +5,689 fuzzy matches. Still a floor (blocking misses cross-block matches). **Net-new = 16,517.**
- **Net-new by revenue band** (shows the opportunity isn't where the headline revenue is): the highest-revenue net-new brands are **non-ICP mega-brands** (Amazon Renewed, Fire TV, Audible, Createspace, Canon, Anker, PlayStation, Bissell, Pearson, Purina, plus a "Generic" $118M/mo bucket). But by count: **48% are $40–100k/mo, 39% $100–500k/mo, 7% $500k–1M, only 6% >$1M/mo.** So ~87% are SMB-plausible; the clear enterprise/Amazon-owned tier is ~6%.
- **Read for Wednesday:** ~55% overlap (not ≥70%) and ~16.5k net-new (well under 50k) → the **SmartScout reverse-lookup bot does not clear Anna's bar**; lean toward the Helium 10 enrichment side. But the *actionable* net-new is smaller than 16.5k once you drop the enterprise tier and the brands we can't resolve to a domain.

### Domain-findability (yield test), 2026-06-23
Sampled 12 mid-band ($100–500k/mo) net-new brands and tried to resolve each to a brand domain:
- **9 of 12 (~75%) had a findable domain** (travelinspira, awiisport, cyndibands, bololo, luvmehair, designmehair, demiwise, reelartpress, exceldryer/XLERATOR) — better than the "low hit rate" worry.
- **3 misses:** generic ambiguous name (bed INC), Amazon-only seller w/ no site (MERXENG), publisher imprint (Atheneum/Caitlyn Dlouhy).
- **Quality caveats:** several "found" are non-ICP — a book publisher, a parent-company domain, generic Chinese-seller brands, and a case where the US contact is a *distributor* not the brand (BOLOLO → Wonderborn LLC). And these were *manual* lookups; the bot's automated name→domain step resolves fewer, then BetterContact still has to find an email.
- **Funnel estimate on 16.5k net-new:** ~75% domain (generous) → automated-resolution + email + ICP cuts → **realistically a few thousand truly-usable new SMB brands.** Small sample (n=12), wide uncertainty.

**Net recommendation for Wednesday:** overlap ~55% (not ≥70%), net-new ~16.5k (well under 50k), usable net-new only a few thousand and quality-mixed → the **SmartScout reverse-lookup bot does not justify the build**; per Anna's rules, lean to the **Helium 10 enrichment** path (refresh/enrich what we have), which is the outside expert's to build.

### What's still missing (next numbers to get)
1. **Decision-grade overlap** — run the fuzzy pass (above). 40% is a floor.
2. **Net-new → usable-contact yield** — of the ~22k net-new (name only), what % can the reverse-lookup resolve to a **domain + deliverable email**? SmartScout gives no domain, so this yield (not the raw 22k) is what actually decides the SmartScout bot's value. *Not yet estimated.*
3. **"Already have" ≠ "usable contact"** — overlap counts brands whose *name* exists in our pool, not whether we hold a current decision-maker email for them.
4. **Exact revenue threshold** — net-new swings 14k–35k across thresholds; need Anna's intended line.
5. **SmartScout data freshness** — `last_seen_at`/`updated_at`; if stale, the "just refreshing numbers" rationale weakens. *Not yet checked.*
6. **Helium 10 side unquantified** — cost-per-lead / accuracy of the managed-API vs Xray-browser routes (Section 3) not yet estimated; and that build is the outside expert's, not Hassan's.

### Ownership — UPDATED 2026-06-25
ClickUp task `86exxwxw7` ("Amazon revenue scraping bot") was originally assigned to **Saad Ali**, but **Hassan is taking the build** (confirmed by Hassan). Per the match test, the build to do is the **Helium 10 enrichment bot** (NOT the SmartScout reverse-lookup) — enrich our existing BetterContact leads with Amazon presence + reseller + revenue. This whole doc is Hassan's build plan now (not a handoff). Re-assign `86exxwxw7` to Hassan in ClickUp.

**Reference:** ClickUp `86exxwxw7` (assignee Saad Ali). Sybill shared link `a667577b-…` is an external share token — not retrievable via our connected Sybill workspace; transcript would need to be pasted in.
