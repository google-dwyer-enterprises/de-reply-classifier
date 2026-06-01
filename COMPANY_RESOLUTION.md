# Company Resolution — Developer Guide

Two scripts that work together to clean up company names for each lead and match them to SmartScout's Amazon brand database. Both are plain-English explainers for new developers.

---

## 1. `resolve_company_names.py` — pick the real company name

### The problem

Each lead has two sources telling us the company name:
- Apollo's guess (`apollo_company_name`)
- Whatever was on the original source list (`company_name`)

They often disagree. E.g. Apollo says `"BR"`, source list says `"Shiseido Brazil"`. We need *one* clean name to show the client in NocoDB.

### The approach

We let Claude (Haiku — cheap model) read both options plus the lead's email domain, and pick the real company.

### How it decides

The prompt gives Claude 5 rules:

1. The email domain usually tells the truth. `john@shiseido.com` → company is Shiseido, ignore the other guess.
2. Except when the domain is gmail/yahoo/etc. — then domain is useless, pick whichever option *looks* like a real business name.
3. Reject obvious junk like `"BR"`, `"EMEA"`, `"Support"`, `"Shop"` — those are subdomain artifacts, not companies.
4. If both look reasonable, prefer the longer/more descriptive one.
5. If both look wrong, fall back to the domain itself, capitalized.

### Optimizations

- **Only ambiguous cases are sent to Claude.** If one side is empty, or both agree, the database's `COALESCE` handles it for free — no API call.
- **Decisions are cached forever.** Once a lead has a `resolved_company_name`, we never re-resolve it. Re-running after a new upload only touches the new ambiguous rows.
- **Batched 25 leads per API call** to keep cost down (pennies per thousand leads).

### Output

Writes `resolved_company_name` + `resolved_company_reason` into `lead_contacts`, then refreshes the materialized view so the client sees the cleaned name in the **"Use this company"** column in NocoDB.

### How to run

```bash
python run.py resolve-companies
```

---

## 2. `smartscout_resolve.py` — match company to a SmartScout brand

### The problem

We have a clean company name per lead (the "Use this company" value from step 1). SmartScout gives us a list of Amazon brands with market data (revenue, category, etc.). We want to know: **which SmartScout brand, if any, is this lead's company?**

Lead company strings are messy — `"Shiseido Brazil"`, `"Shiseido, Inc."`, `"SHISEIDO CO LTD"` should all match the SmartScout brand `"Shiseido"`.

### The approach

**Fuzzy string matching** using the `rapidfuzz` library — no LLM, no API calls, just pure string similarity scoring. Fast and free.

### How it decides

1. Compute a "use this company" string per lead in SQL — same logic the NocoDB view uses (prefer Apollo when its name overlaps the email domain, otherwise use the source-list name).
2. Normalize both sides (lowercase, strip punctuation/spaces) so `"Shiseido, Inc."` and `"shiseido inc"` look identical.
3. For each unique lead company, find the closest SmartScout brand using `token_set_ratio` (a similarity score 0–100 that handles word reordering and extra words well).
4. Accept the match only if the score is **≥ 92**. Below that → leave unmatched (a separate LLM script handles the grey zone).

### Guardrails (learned the hard way)

- **Drop SmartScout brands with normalized length < 3.** Single-letter brand names like `"r"` or `"u"` match everything.
- **Length-ratio guard.** Reject a match if the brand string is less than 40% the length of the lead string. Stops `"amazon"` from matching `"Inboostr | Amazon Solutions Partner"` — the brand is way shorter than the lead, so it's almost certainly a sub-phrase coincidence, not the real company.
- **Dedup before matching.** If 50 leads all share the company `"Shiseido"`, we fuzzy-match once and fan the result out. Big speedup on large datasets.

### Output

Upserts into `lead_smartscout_match`:
- `brand_norm` — the matched brand (NULL if no match)
- `match_score` — the fuzzy score (kept even for misses, so the LLM pass can target a specific score band)
- `match_method` — `'fuzzy'` or `'none'`
- `use_this_company` — the lead company string we matched against (audit trail)

Then refreshes the materialized view so NocoDB shows the linked SmartScout data.

### How to run

```bash
python run.py resolve-smartscout            # only unresolved leads (default)
python run.py resolve-smartscout --rerun    # re-match everyone (after tweaking thresholds)
```

Auto-runs at the end of `upload-leads`, so day-to-day you usually don't have to call it manually.

### What about the grey zone?

Leads scoring 85–92 are ambiguous — `rapidfuzz` isn't confident, but there's *probably* a real match. `smartscout_llm_resolve.py` is a separate manual script that sends grey-zone leads to Claude Haiku for a semantic second opinion. Costs about $1 per 3.5k leads. See `CLAUDE.md` for that workflow.

---

## How they chain together

```
lead_contacts (raw Apollo + source-list names)
    │
    │  resolve_company_names.py   (LLM, fixes disagreements)
    ▼
lead_contacts.resolved_company_name
    │
    │  (MV's "Use this company" column = COALESCE chain)
    ▼
clean company string per lead
    │
    │  smartscout_resolve.py   (fuzzy match to brand list)
    ▼
lead_smartscout_match
    │
    │  (optional, manual) smartscout_llm_resolve.py   (LLM, grey zone)
    ▼
NocoDB sees: lead → cleaned company → SmartScout brand → market data
```
