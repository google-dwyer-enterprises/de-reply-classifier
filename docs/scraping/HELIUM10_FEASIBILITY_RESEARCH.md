# Helium 10 / Amazon Revenue Bot — Feasibility Research (cited)

**Date:** 2026-06-30 · **Method:** deep-research workflow `wf_8b874e7e` (6 angles, 24 sources fetched, 75 claims extracted, 25 adversarially verified → 19 confirmed / 6 killed). · **Status:** durable record — keep local with the Amazon bot docs.

**Question researched:** feasibility of an Amazon brand-revenue QA bot that gates leads at a coarse **$500k/yr** threshold, using **Helium 10 web tools (Black Box) via Playwright browser automation** on a **persistent logged-in session** (one-time Google login on a **VNC/RDP VM**) — vs alternatives (SmartScout, Jungle Scout, Keepa, managed Amazon-data APIs).

---

## Executive summary (the verdict)

The "Helium 10 web tools via a persistent logged-in browser on a VNC VM" approach is **technically buildable but NOT a reliable or low-risk way** to get brand-level Amazon revenue for a coarse gate. The *data capability* exists (Black Box "Exact Brand Search" enumerates a brand's products, Platinum tier), but **two structural problems** undermine the automation plan:
1. **Helium 10's ToS explicitly prohibits automated access** and reserves the right to suspend/terminate without notice.
2. **Google blocks software-automated sign-ins**; only a one-time *manual* login + persisted session works, and that session **has no auto-reauth** — it expires and needs periodic **manual re-login via VNC** (so it isn't truly unattended).

Additionally, **no source verified that Black Box's revenue estimates are accurate enough to gate on**, and its exact data-freshness (days stale) **could not be confirmed**.

**Recommendation:** for a coarse $500k gate at modest volume, **stay on SmartScout** (already integrated, no ToS/automation risk; refresh the export for freshness); optionally add a **managed Amazon-data API (e.g. Rainforest)** for presence/gap revenue *after* confirming it returns revenue estimates. **Shelve the Helium 10 UI automation.**

---

## Confirmed findings (adversarially verified)

**1. Black Box can enumerate all products under a brand/seller** — *high, 3-0.*
"Exact Brand Search" / "Exact Seller Search" in the Competitors section "pulls up all the products sold under that specific brand or by that seller," in a grid filterable by monthly units sold. *Caveat:* bounded by DB coverage + possible row caps; summing per-product → brand total is an **inference**, not a one-click feature.
Source: kb.helium10.com/hc/en-us/articles/29666912970651 (primary).

**2. Black Box needs Platinum tier; pricing** — *high, 3-0.*
Platinum = **$129/mo monthly, $99/mo billed annually**; Diamond $279–359/mo; Enterprise from $1,499/mo. Free/Starter give only limited lifetime searches. *Caveat:* $129 reflects the **April 2026 price increase**; Starter discontinued for new signups Apr 2026 → Platinum is the current entry tier for full Black Box.
Sources: helium10.com/pricing (primary), helium10.com/tools/product-research/black-box (primary), revenuegeeks.com (corroborating).

**3. Helium 10 ToS prohibits automated access** — *high, 3-0.*
ToS §2(C) Prohibited Uses: "access (or attempt to access) this site through any automated means (including use of scripts or web crawlers)." §4: "We may terminate this Agreement without notice or… temporarily suspend your access… in the event that you breach." → The Playwright plan is a **direct ToS breach** exposing the account to suspension. *Caveat:* establishes the right to ban, not the observed enforcement rate.
Source: helium10.com/terms-and-conditions (primary).

**4. Google blocks software-automated sign-ins** — *high, 3-0.*
Google's support doc lists "controlled through software automation rather than a human" and "embedded in a different application" among conditions where it stops sign-ins; practitioners confirm detection via `navigator.webdriver`, TLS fingerprints, timing → CAPTCHAs/bans. *Caveat:* the load-bearing risk is the "software automation" detection (Playwright drives standalone Chromium, so the embedded-app clause is less relevant) — which is why the plan proposes a **one-time human login**, not scripted OAuth.
Sources: support.google.com/accounts/answer/7675428 (primary), adequatica.medium.com (practitioner).

**5. Only viable auth = one-time manual login, then persist the session** — *high, 3-0.*
"Don't try to log in with username/password from a bot. Helium 10 will block it… sign in once by hand, capture the session cookies… load those on every run" (covers helium10.com + members.helium10.com). Matches Playwright's official auth pattern. *Caveat:* supports the **auth** route only; the broader "drive H10 reports via automation" claim FAILED verification (1-2).
Sources: axiom.ai/automate/helium10-login (blog), playwright.dev/docs/auth (primary).

**6. Playwright can persist an authenticated session** — *high, 3-0.*
Via `storageState()` (cookies/localStorage; IndexedDB opt-in since v1.51) or `launch_persistent_context(user_data_dir=…)`. *Caveats:* session-only cookies are NOT written to disk by design (GitHub #36139); can't run two instances on one userDataDir; **disk persistence does NOT extend the remote server's session** — server-side expiry/re-auth is a separate uncontrollable failure mode.
Sources: playwright.dev/docs/auth + class-browsertype (primary), dev.to, medium.

**7. Playwright has NO auto-refresh of expired auth** — *high, 3-0.*
Official docs: "you need to delete the stored state when it expires." Developers must build their own TTL/401-detection/re-auth. For an unattended bot → **periodic manual human re-login via VNC is structurally required**; it cannot self-heal. App sessions often expire ~24h; stealth plugins (puppeteer-extra-plugin-stealth) unmaintained since Mar 2023.
Source: playwright.dev/docs/auth (primary).

**8. Rainforest API = real-time, web-scraped public Amazon data** — *high, 3-0.*
"Not affiliated or endorsed by Amazon… does not make use of any Amazon API… web-scraped data from public domain sources," rendered in a real-time in-memory browser. *Critical caveat:* whether it returns **per-brand/per-product revenue estimates** (needed for the gate) is **UNRESOLVED** — see open questions.
Source: trajectdata.com/ecommerce/rainforest-api (primary/vendor).

---

## Refuted claims (verified FALSE — do not rely on these)

- ✗ "Black Box is included in Platinum/Diamond/Enterprise (not Free)" — *0-3* (nuance wrong vs the verified tier statement).
- ✗ "Rainforest API does NOT provide revenue/sales estimates" — *0-3* (so it may or may not; unresolved).
- ✗ "Platinum $79 / Diamond $229 / Starter $29" — *0-3* (**stale pricing**, pre-Apr-2026).
- ✗ "Platinum $99/mo, $79 annual" — *0-3* (**stale**).
- ✗ "Helium 10 web tools can be driven by automation running reports the user would run manually" — *1-2* (broad automation viability **not established**).
- ✗ "After initial login, subsequent runs need no re-auth (persistent profile reused)" — *0-3* (re-auth WILL be needed).

---

## Caveats

- **Time-sensitivity:** Helium 10 pricing reflects the April 2026 increase; treat any sub-$99 Platinum figure as stale.
- **Source quality:** data-capability, pricing, ToS, and Google-login claims rest on **primary** sources (high confidence). Anti-detection tactics + "drive H10 reports via automation" rest on single blogs — and the broad-automation claim FAILED verification.
- **No real-world enforcement rate:** the ToS establishes the right to ban; no source quantifies how often Helium 10 actually bans a low-volume persistent-session bot. ToS risk is contractual, enforcement-frequency unknown.
- **Revenue-accuracy-for-gating unverified:** the *enumeration* capability is confirmed, but whether Black Box's per-product revenue (summed) is reliable enough for even a coarse $500k gate was **not** verified.
- **Alternatives under-researched:** SmartScout/Jungle Scout/Keepa were not deeply verified beyond Rainforest, so the "recommendation among alternatives" is directional.

---

## Open questions (need direct/primary confirmation)

1. Does Black Box (or Xray) expose **per-product monthly revenue** that sums to a defensible brand total, and how stale/accurate is it? (Black Box refresh cadence, "data as of", trailing-30-day basis vs live Xray — **not answered**.)
2. Does **Rainforest** (or Keepa) actually return per-brand/per-product **revenue estimates** sufficient for a $500k gate, and at what price/volume?
3. **Real-world enforcement:** any documented Helium 10 suspensions for low-volume persistent-session automation, vs the ToS merely reserving the right?
4. Since **SmartScout is already integrated** (`smartscout_brands` / `smartscout_resolve`), how do SmartScout & Jungle Scout compare on brand-revenue coverage/freshness/cost/licensing for this coarse gate — **is a Helium 10 route even necessary?**

---

## Sources (24 fetched; primary = highest weight)

**Primary:** kb.helium10.com/…/29666912970651 · helium10.com/pricing · helium10.com/tools/product-research/black-box · helium10.com/terms-and-conditions · support.google.com/accounts/answer/7675428 · playwright.dev/docs/auth · playwright.dev/docs/api/class-browsertype · trajectdata.com/ecommerce/rainforest-api
**Blog/secondary:** axiom.ai/automate/helium10-login · adequatica.medium.com/google-authentication-with-playwright · revenuegeeks.com (starter, black-box) · smartscout.com/blog/smartscout-data-collection-estimation-process · flybyapis.com/blog/rainforest-api-alternatives · dev.to/amals367 · teemutaskula.com · medium.com/@Gayathri_krish · roundproxies.com/blog/authentication-playwright
**Forum:** github.com/microsoft/playwright/issues/36139

---

## Addendum — Do Rainforest / Keepa return BRAND revenue? (verified 2026-06-30)

**Bottom line: NO — neither returns brand-level revenue directly. Both are per-ASIN; you'd have to enumerate a brand's ASINs and assemble the total yourself.** Of the two, **Keepa is the better fit** if we ever go paid (it can filter by brand + gives Amazon's *real* "bought past month" + is cheap + is a legitimate API). Rainforest gives per-ASIN *unit* estimates only.

**Rainforest API — `type=sales_estimation`:**
- Per-ASIN (or per `bestseller_rank`) response fields: `has_sales_estimation`, `monthly_sales_estimate`, `weekly_sales_estimate`, `bestseller_rank`, `sales_estimation_category`.
- `monthly_sales_estimate` = **UNITS per month** (e.g. 7921) — **NOT revenue**. Derived from product page + BSR + reviews + search rankings + known datapoints.
- **No brand aggregation** — per-ASIN only. Brand total = enumerate the brand's ASINs → × price → sum (build it yourself).
- Pricing: credit plans ~**$83–$9,000/mo** (billed annually).
- Sources: rainforestapi.com/docs/product-data-api/results/sales-estimation ; trajectdata.com/ecommerce/rainforest-api/sales-estimation.

**Keepa API:**
- `monthlySold` = "how often bought in the past month" — this is **Amazon's own 'bought past month' number, NOT a model estimate** (higher quality than pure BSR models). Present only when Amazon surfaces it (missing for low-volume ASINs). Also `salesRankDrops30/90/180/365` as sale indicators.
- **Product Finder CAN filter by brand** (almost all product fields are searchable/sortable; returns a Search Insights summary aggregating KPIs incl. brand counts) → it *can* enumerate a brand's ASINs.
- **No revenue field** — revenue = `monthlySold × price`, summed across the brand's ASINs (you build the rollup).
- Pricing: token-based — **€49/mo (~$54)** = 20 tokens/min (1 token = 1 product's full data), up to €4,499/mo. Cheap at low volume.
- **Legitimate API → no ToS/automation/ban risk** (unlike scripting Helium 10's UI).
- Sources: keepa.com/#!api ; keepaapi.readthedocs.io/en/latest/api_methods.html ; github.com/keepacom/api_backend (Product.java, Stats.java) ; revenuegeeks.com/keepa-pricing.

**Verdict for our coarse $500k/yr gate:**
1. **SmartScout (already integrated)** remains the only source that hands us **brand revenue directly** (no ASIN enumeration/rollup) — simplest path; just refresh the export for freshness.
2. If we want fresher/broader coverage **without** Helium 10's ToS/ban risk, **Keepa is the best paid add-on**: ~$54/mo, legitimate API, brand Product Finder + Amazon's real `monthlySold`. Cost = building the ASIN-enumeration + revenue-rollup (≈ what SmartScout already does for us).
3. **Rainforest** is weaker for this: units-only estimate, no brand enumeration, pricier entry.
4. **Net ranking:** SmartScout (for the gate now) → Keepa (if a fresher paid source is wanted later) → Rainforest → Helium-10-UI-automation (avoid).

---

## Addendum 2 — SmartScout data source & freshness (verified 2026-06-30)

- **Source:** SmartScout monitors/collects **public Amazon marketplace data** — brands, sellers, products, subcategories (listings, sales rank/BSR, category data, brand/seller storefronts). It is **not** Amazon's internal API; the exact collection mechanism is undisclosed. Revenue is a **BSR→units×price estimate aggregated to brand/seller** (same family as Helium 10 / Jungle Scout).
- **SmartScout's own update cadence:** **daily** for most metrics (brands/sellers/products/subcategories), some (traffic) weekly, "a few hours after available on Amazon."
- **Accuracy:** vendor claims ~80–90% (≈ within 15–25% of actual), **not independently audited**; directional — good for ranking/comparing brands, weaker as a precise per-product forecast. **Fine for a coarse $500k gate.**
- **⚠️ CRITICAL for us:** SmartScout refreshes daily *on their platform*, but **our `smartscout_brands` table is a STATIC snapshot loaded ~2026-05-08** — it does NOT auto-update. Our gate's freshness depends entirely on **how often we re-export from SmartScout and re-upload** (`run.py upload-smartscout`). So "keep SmartScout fresh" = schedule a periodic re-export/re-upload (or use SmartScout's API), not something that happens automatically.
- Sources: revenuegeeks.com/smartscout-review ; smartscout.com/smartscout-data-levels ; smartscout.com (brand data/API pages).

---

## Addendum 3 — Does SmartScout offer an API for FRESH revenue? (verified 2026-06-30)

**YES — and it's the cleanest fresh-brand-revenue source of everything researched.**
- The SmartScout API returns **brand-level data directly, including estimated revenue** ("pull detailed profiles for any brand: estimated revenue, number of products, average sellers per product…") — plus products, sellers, subcategories, keywords, and competitor/ad data. Endpoints let you **query brands by revenue/growth**; a **Data Lake** option supports bulk historical + real-time dumps. Supports marketplace params, pagination, sorting, filtering.
- **This solves our staleness problem cleanly:** fresh brand revenue on demand, automated, **no ASIN enumeration/rollup** (unlike Keepa/Rainforest), and it's a **legitimate API — no ToS/ban risk** (unlike Helium 10 UI scraping).
- **Catch — cost/access:** API is an **add-on / custom Enterprise tier**, NOT in the standard plans ($25–$187/mo). Enterprise ~**$399+/mo** (bundles API + Data Lake + historical + ad-spend); exact API price is **custom → contact sales / schedule a demo**. Public rate limits not stated.
- Sources: smartscout.com/amazon-selling-guides/…smartscout-api-guide ; revenuegeeks.com/smartscout-api ; smartscout.com/pricing.

### Freshness options for the gate — ranked
1. **Manual SmartScout re-export → `run.py upload-smartscout`, periodically** — cheapest (uses our existing plan/data), gives direct brand revenue, but manual. Fine for a coarse gate refreshed ~monthly.
2. **SmartScout API (Enterprise add-on, ~$399+/mo)** — automated, direct fresh brand revenue, no assembly, no ToS risk. Best if budget allows + we want hands-off freshness.
3. **Keepa API (~$54/mo)** — cheap + legitimate, but we build the brand ASIN-enumeration + `Σ(monthlySold×price)` rollup ourselves.
4. Helium 10 UI automation — avoid (ToS/ban/reauth).
