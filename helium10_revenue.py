"""Helium 10 revenue provider — the ACCURATE layer of the Amazon Revenue QA cascade.

Net recommendation (see AMAZON_REVENUE_QA_BOT_INSTRUCTIONS.md D7): use Helium 10's
**web tools** (query H10's own site — NO Amazon pages, so no Amazon CAPTCHA) through a
**persistent logged-in browser profile** (log in once with google@dwyer-enterprises.com;
the bot reuses the session). Durable + low-maintenance vs. exported cookies (which expire
and may miss the SPA's localStorage token).

CONFIG (env) — pick ONE connection mode:
  H10_USER_DATA_DIR : path to a Chrome profile already logged into Helium 10
                      (persistent-context mode — recommended for a dedicated VM).
  H10_CDP_URL       : CDP endpoint of a long-running logged-in Chrome
                      (connect-over-CDP mode — good when a browser is already up).
  H10_TOOL_URL      : the H10 tool to read brand revenue from (default = Black Box
                      products). ** Confirm the exact tool/URL in the live account. **
  H10_HEADLESS=0    : show the browser (debugging).
  H10_DEBUG=1       : dump page HTML + screenshot to exports/ so the operator can
                      identify the brand-filter + revenue selectors.

FAIL-SAFE: returns None on anything uncertain (not configured, playwright missing,
session not logged in, or revenue not confidently parsed) so the cascade routes the
brand to REVIEW and never gates on a guessed number.
"""
from __future__ import annotations

import os
import re

H10_USER_DATA_DIR = os.environ.get("H10_USER_DATA_DIR")
H10_CDP_URL = os.environ.get("H10_CDP_URL")
H10_TOOL_URL = os.environ.get("H10_TOOL_URL", "https://members.helium10.com/black-box/product-research")
H10_TIMEOUT_MS = int(os.environ.get("H10_TIMEOUT_MS", "45000"))
H10_HEADLESS = os.environ.get("H10_HEADLESS", "1") != "0"
H10_DEBUG = os.environ.get("H10_DEBUG") == "1"


def configured() -> bool:
    return bool(H10_USER_DATA_DIR or H10_CDP_URL)


def _money_to_float(s: str) -> float | None:
    """'$1,234' / '$1.2M' / '$950K' -> float."""
    if not s:
        return None
    m = re.search(r"\$?\s*([\d,]+(?:\.\d+)?)\s*([KMB])?", s, re.I)
    if not m:
        return None
    val = float(m.group(1).replace(",", ""))
    mult = {"k": 1e3, "m": 1e6, "b": 1e9}.get((m.group(2) or "").lower(), 1)
    return val * mult


def revenue_for_brand(brand: str) -> dict | None:
    """Brand -> {annual_revenue, country, on_amazon, source='helium10'} or None."""
    if not configured():
        return None
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None  # playwright not installed on this host -> cascade -> REVIEW

    try:
        with sync_playwright() as p:
            ctx = None
            if H10_CDP_URL:
                browser = p.chromium.connect_over_cdp(H10_CDP_URL)
                ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            else:
                ctx = p.chromium.launch_persistent_context(H10_USER_DATA_DIR, headless=H10_HEADLESS)
            page = ctx.new_page()
            page.goto(H10_TOOL_URL, timeout=H10_TIMEOUT_MS)
            page.wait_for_load_state("networkidle", timeout=H10_TIMEOUT_MS)
            # Session check: if H10 bounced us to a login/SSO page, the profile isn't
            # logged in -> bail (operator must re-login on the profile).
            if re.search(r"(login|sign[-_]?in|sso|auth)", page.url, re.I):
                _debug_dump(page, brand, "not-logged-in")
                return None
            annual = _query_and_sum(page, brand)
            if H10_DEBUG:
                _debug_dump(page, brand, "after-query")
            if annual is None:
                return None
            return {"annual_revenue": annual, "country": None,
                    "on_amazon": True, "source": "helium10"}
    except Exception:
        return None


def _query_and_sum(page, brand: str) -> float | None:
    """Drive the H10 tool: filter by brand, read each product's MONTHLY revenue, sum,
    x12 -> annual.

    ** TWO selectors must be confirmed inside the live Helium 10 account ** (they
    differ by tool/version and can't be guessed blind):
      1. BRAND_FILTER_SELECTOR — the Black Box "Brand" filter input.
      2. REVENUE_CELL_SELECTOR  — the per-product "Revenue"/"Monthly Revenue" cell.
    Set them below (or via env H10_BRAND_SELECTOR / H10_REVENUE_SELECTOR), then this
    returns a real number. Until confirmed it returns None (fail-safe -> REVIEW).
    """
    brand_sel = os.environ.get("H10_BRAND_SELECTOR")        # TODO: confirm in live UI
    rev_sel = os.environ.get("H10_REVENUE_SELECTOR")        # TODO: confirm in live UI
    if not (brand_sel and rev_sel):
        # Selectors not yet confirmed — do not guess revenue.
        return None
    try:
        page.fill(brand_sel, brand)
        page.keyboard.press("Enter")
        page.wait_for_load_state("networkidle", timeout=H10_TIMEOUT_MS)
        cells = page.query_selector_all(rev_sel)
        monthly = [v for v in (_money_to_float(c.inner_text()) for c in cells) if v]
        if not monthly:
            return None
        return sum(monthly) * 12.0
    except Exception:
        return None


def _debug_dump(page, brand: str, tag: str) -> None:
    if not H10_DEBUG:
        return
    try:
        safe = re.sub(r"[^a-z0-9]+", "_", brand.lower())[:30]
        page.screenshot(path=f"exports/h10_{safe}_{tag}.png", full_page=True)
        with open(f"exports/h10_{safe}_{tag}.html", "w", encoding="utf-8") as f:
            f.write(page.content())
    except Exception:
        pass
