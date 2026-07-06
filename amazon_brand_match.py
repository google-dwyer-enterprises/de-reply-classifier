"""Strict brand matcher for the Amazon Revenue QA bot.

WHY: the existing lead_smartscout_match uses rapidfuzz token_set_ratio, which scores
100 whenever the company merely CONTAINS the brand word — so "Blue Apple Co.",
"The Good Apple", even "NAF NAF" all matched brand "apple" ($2.9B/yr) and would
wrongly PASS the revenue gate. We cannot gate revenue on that.

STRICT ORDER (returns the first confident hit, else grey-zone LLM, else None):
  1. EXACT  — normalize_brand(company) == brand_norm.                      conf=100
  2. FUZZY  — token_sort_ratio (NOT token_set_ratio, so extra company words
              hurt the score) over a 3-char-prefix candidate block, with a
              length guard. >= FUZZY_HIGH is accepted.                      conf=score
  3. GREY   — FUZZY_LOW..FUZZY_HIGH: ask Haiku "is <company> the Amazon brand
              <brand>?" (only when use_llm=True). yes -> accept.            conf=score
  4. none.

token_sort_ratio kills the containment false-positives: "apple blue co" vs "apple"
scores ~55, not 100, so it never reaches FUZZY_HIGH.
"""
from __future__ import annotations

from rapidfuzz import fuzz, process

from smartscout_upload import normalize_brand

FUZZY_HIGH = 90    # >= this => accept without LLM
FUZZY_LOW = 78     # FUZZY_LOW..FUZZY_HIGH => grey zone (LLM disambiguation)
MIN_LEN = 3        # ignore ultra-short normalized names (too ambiguous)


def _candidates(cur, nc: str) -> list[str]:
    """SmartScout brand_norms sharing the first 3 chars (blocking) — the pool we
    fuzzy-score against, so we never scan all ~275k brands per company."""
    cur.execute(
        "select brand_norm from smartscout_brands where brand_norm like %s",
        (nc[:3] + "%",),
    )
    return [r[0] for r in cur.fetchall() if r[0]]


def _length_guard(nc: str, brand: str) -> bool:
    """Reject when the brand is much shorter/longer than the company — guards
    against a tiny brand token matching a long company name."""
    a, b = len(nc), len(brand)
    return min(a, b) / max(a, b) >= 0.45 if max(a, b) else False


def _llm_same_brand(company: str, brand: str) -> bool:
    """Haiku yes/no: is this company the same business as the Amazon brand?
    Used only for the grey zone. Returns False on any error (fail-safe = no match)."""
    try:
        import os
        from anthropic import Anthropic
        client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        import classify
        sys_p = ("You decide if a company is the SAME business as an Amazon brand. "
                 "Answer ONLY 'yes' or 'no'. Different companies that share a common "
                 "word (e.g. 'Blue Apple Co.' vs brand 'Apple') are 'no'.")
        user = f"Company: {company!r}\nAmazon brand: {brand!r}\nSame business?"
        out = classify.call_haiku(client, sys_p, user, model="claude-haiku-4-5-20251001")
        return (out or "").strip().lower().startswith("y")
    except Exception:
        return False


def match_brand(cur, company: str, use_llm: bool = False) -> dict | None:
    """Resolve a company name to a SmartScout brand_norm, strictly.
    Returns {brand, method, confidence} or None."""
    nc = normalize_brand(company)
    if not nc or len(nc) < MIN_LEN:
        return None
    # 1. exact normalized match
    cur.execute("select 1 from smartscout_brands where brand_norm = %s limit 1", (nc,))
    if cur.fetchone():
        return {"brand": nc, "method": "exact", "confidence": 100.0}
    # 2/3. guarded fuzzy over the prefix block
    cands = _candidates(cur, nc)
    if not cands:
        return None
    best = process.extractOne(nc, cands, scorer=fuzz.token_sort_ratio)
    if not best:
        return None
    brand, score, _ = best
    if not _length_guard(nc, brand):
        return None
    if score >= FUZZY_HIGH:
        return {"brand": brand, "method": "fuzzy", "confidence": float(score)}
    if score >= FUZZY_LOW and use_llm and _llm_same_brand(company, brand):
        return {"brand": brand, "method": "llm", "confidence": float(score)}
    return None


if __name__ == "__main__":
    # quick check that the strict matcher kills the 'apple' false positives
    from db import connect
    conn = connect(); cur = conn.cursor()
    tests = ["Blue Apple Co.", "The Good Apple", "Green Apple", "NAF NAF",
             "Mrs. Prindable's", "Apple Rubber", "OLAPLEX", "Bombshell Sportswear"]
    print(f"{'COMPANY':<26} -> MATCH")
    for t in tests:
        m = match_brand(cur, t, use_llm=False)
        print(f"  {t:<26} -> {m}")
    conn.close()
