# BetterContact Lead-Quality Remediation — Findings & Plan

**Status:** P1, P2, P3 committed. P5 (QA gate + `qa-leads` CLI) implemented.
DB cleaned: BetterContact 564 / Prospeo 1,417 accepted after quarantining all
prohibited + service + (BC) LLM-caught reseller/agency/marketplace leads; the
Jun-3 BetterContact export was re-cut to a clean 538-brand sheet. P4 still
blocked pending a product decision (see §3). Remaining: a known-FP **allowlist**
(Paul Mitchell, Aloxxi, Shurtape, Zebra Athletics, Louisville Slugger, Vortex)
so the deterministic gate stops flagging those legit product brands — was
deliberately deferred, so the gate still reports those 9 rows.
**Trigger:** Audit found Hassan's BetterContact batch full of irrelevant companies
(cannabis, acupuncture, service businesses). Jamie paused use of all of Hassan's
scraped lists until a QA process is in place. New requirement from Jamie:
*exclude cannabis, liquor, and guns; make sure every domain actually sells products.*

---

## 0. Confidence statement (read this first)

I am **not claiming the plan is "100% guaranteed to fix it."** No lead filter
removes every bad record. What I *am* confident in, because each point was
verified against the production `prospeo_new_leads` table and the API probe
scripts, is:

1. The **diagnosis** of why BetterContact leads are garbage and Prospeo's were not.
2. That the proposed fixes **close those specific gaps** and satisfy Jamie's
   product-category requirement.

"Fixed" becomes a **defensible claim only after a measured QA pass** on a held-out
sample shows the garbage rate has dropped below an agreed threshold. The plan
therefore ends in measurement, not assertion. We do **not** report "fixed" to
Jamie before that number exists.

---

## 1. The core misconception

The work was done believing BetterContact used "the same params and filters" as
Prospeo. **It does not.** BetterContact's pipeline silently drops the two filters
that did essentially all of Prospeo's quality work. That — not BetterContact the
vendor — is the root cause.

---

## 2. Verified findings

### 2.1 Prospeo's quality came from four layers; BetterContact keeps only the weakest two

| Quality layer | Prospeo | BetterContact |
|---|---|---|
| Industry filter | yes | yes (shared list) |
| **Revenue floor (≥ $500K)** | **yes, on every search** | **none** — replaced by a coarse headcount≥5 proxy |
| Title filter | curated, server-side smart-match | looser substring match + local post-filter |
| **LLM brand / agency / reseller / marketplace gate** | **yes — rejects anything not a product brand** | **absent — every lead hard-coded "accepted"** |

### 2.2 The numbers (live data)

- **Acceptance rate exposes it.** Prospeo rejected **32.5%** of leads (715 of
  2,201): the LLM gate alone threw out 344 resellers, 289 agencies, 7
  marketplaces, 43 unknown. BetterContact rejected **1.7%** (19 of 1,136) — and
  **100% of its accepted leads bypassed the LLM gate entirely.**
- **The garbage is exactly what the missing gates would have caught:** dog
  daycare/training/boarding franchises, animal inns, IV-drip and acupuncture
  clinics, multi-brand resellers — none of which sell their own product.

### 2.3 The claim we must NOT make to Jamie

**Prospeo did not filter out cannabis, liquor, or guns either.** Verified:
Prospeo's own accepted leads include major cannabis companies (Curaleaf, MariMed,
Verano, Lowell Herb Co), and cannabis/gun keyword counts are comparable across
both tools. Reason: a dispensary *does* sell its own product, so the LLM gate
labels it a "brand" and accepts it.

➡️ **Accurate framing:** Cannabis/liquor/guns slipping through is a **pre-existing
gap in both tools**, not a BetterContact regression. A prohibited-category
exclusion has **never existed** and is genuinely new work. What made the
BetterContact batch look uniquely bad is the *missing quality gates* letting a
flood of resellers and service businesses through *on top of* that shared gap.

### 2.4 The single biggest source of garbage is one industry

The shared industry list includes **"Alternative Medicine."** Of BetterContact's
155 accepted leads in it: **~83% are cannabis, ~9% more are clinics/spas**, and
only ~8% are plausibly legitimate product brands (most of which also appear under
other industries). This one industry is the highest-leverage thing to remove.

### 2.5 BetterContact gives us a stronger signal than Prospeo — currently discarded

BetterContact returns a rich `company_description` **and** a `company_keywords`
list for nearly every lead, but the current code throws both away. These catch
coy-named cannabis brands (710 Labs, Muha Meds, Curaleaf) that have no giveaway
in the company name — a name-only blocklist misses them, but a
keywords+description check caught 8 of 9 in testing. This makes a reliable
category filter feasible.

---

## 3. Proposed solution plan

Ordered by impact-to-effort. Each item also applies to Prospeo where relevant,
since several gaps are shared.

**P1 — Remove the worst-offending industry.**
Drop (or quarantine) "Alternative Medicine" from the shared industry list.
Highest-precision single change; eliminates the bulk of the cannabis/clinic
problem immediately. Re-examine the other service-heavy industries (e.g. Pet
Services) for the same issue.

**P2 — Add a prohibited-category exclusion (Jamie's new rule).**
Introduce an explicit cannabis / liquor / firearms blocklist, evaluated against
the rich signals (keywords + description), not just the company name. Applies to
both providers. This is net-new capability neither pipeline ever had.

**P3 — Restore a "must sell its own product" gate on BetterContact.**
Surface the description/keywords BetterContact already returns and run them
through the same brand-vs-agency-vs-reseller-vs-service classification Prospeo
uses, rejecting anything that isn't a product brand. This is what closes the
service-business / reseller flood.

**P4 — Size/revenue floor for BetterContact — BLOCKED, needs a decision.**
Reassessed against the data and the original approach does not hold:
- BetterContact returns **no revenue field**, and its size fields
  (`company_size`, `company_employees`, `company_funding`, …) are **0% populated**
  (1,136/1,136 null). There is no local size data to filter on.
- SmartScout **cannot** serve as a hard floor: it only covers Amazon sellers, so
  a non-match means "not on Amazon," not "too small" — using it to reject would
  delete every legitimate non-Amazon DTC brand.
- The only real lever is the server-side `company_headcount_min`. R&D shows
  raising it from 5 to 20 cuts ~44% — but a headcount floor is **not** equivalent
  to Prospeo's revenue floor: it would drop lean, high-revenue DTC brands (e.g. an
  8-person, $2M cosmetics brand) that are *exactly* Hassan's ICP.

Recommendation: **do not bluntly raise the headcount floor.** Most of what P4 was
meant to remove (hobby shops, dropshippers, service businesses) is already caught
by P2+P3 and the deliverable-email requirement. Options to decide between: (a)
keep `headcount_min=5`, treat SmartScout revenue as a positive prioritization
signal only (not a reject); (b) a modest headcount bump accepting some ICP loss;
(c) defer P4 and let the P5 QA gate measure whether a size floor is even needed.

**P5 — Add a pre-export QA gate (this is the deliverable Jamie actually asked for).**
Before any list is exported, automatically sample the accepted leads, measure the
garbage rate (prohibited categories + non-product businesses), and **block the
export if it exceeds an agreed threshold.** This is what lets the lists be
un-paused with confidence and prevents a repeat.

---

## 4. Acceptance criteria (definition of "fixed")

- A measured QA pass on a fresh sample shows the prohibited-category rate at ~0%
  and the non-product-business rate below the agreed threshold.
- The pre-export QA gate (P5) is live and blocking on failure.
- Only then is the pause lifted and "fixed" reported to Jamie — with the measured
  number attached, not a promise.

## 5. Scope note — existing data

Both tools' historical exports already contain cannabis, clinic, and reseller
leads. A one-time cleanup pass over the already-accepted rows is needed in
addition to fixing the pipeline going forward; otherwise previously exported lists
remain contaminated.
