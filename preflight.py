"""preflight.py — verify third-party providers are healthy BEFORE a scrape spends.

A scrape depends on BetterContact (enrichment) and, for a revenue-first batch,
Rainforest (the Amazon revenue gate). If a dependency is down/degraded, starting
the batch only wastes credits — exactly what happened when BetterContact's enrich
endpoint hung and batches #48–50 burned Rainforest gating for 0 accepted leads.

This gates two places:
  * the submit form  — refuse to queue a batch when a needed provider is down;
  * the worker       — refuse to START spending; leave the batch pending so it
                       runs automatically once the provider recovers.

Results are cached for PROBE_TTL_S so the worker doesn't re-probe (and re-spend a
BetterContact credit) on every poll while a batch waits.
"""
from __future__ import annotations

import os
import time

PROBE_TTL_S = 300          # reuse a health result for 5 min
BC_PROBE_TIMEOUT_S = 60    # a BC enrich that can't finish in 60s = degraded
# Plausible names at real domains — the probe only needs BetterContact's async
# poll to TERMINATE quickly (that's what hangs when it's degraded); whether it
# actually finds an email is irrelevant, so this costs ~0 credits.
_BC_PROBE = [
    {"first_name": "John", "last_name": "Smith", "company_domain": "nike.com", "company_name": "Nike"},
    {"first_name": "Jane", "last_name": "Doe", "company_domain": "shopify.com", "company_name": "Shopify"},
    {"first_name": "Sam", "last_name": "Lee", "company_domain": "hubspot.com", "company_name": "HubSpot"},
]

_cache: dict[str, tuple[float, bool, str]] = {}


def _cached(name: str, fn):
    now = time.time()
    hit = _cache.get(name)
    if hit and now - hit[0] < PROBE_TTL_S:
        return hit[1], hit[2]
    ok, msg = fn()
    _cache[name] = (now, ok, msg)
    return ok, msg


def _rainforest() -> tuple[bool, str]:
    key = os.environ.get("RAINFOREST_API_KEY")
    if not key:
        return True, "Rainforest not configured (skipped)"
    try:
        import requests
        r = requests.get("https://api.rainforestapi.com/account",
                         params={"api_key": key}, timeout=20)
        rem = (r.json().get("account_info") or {}).get("credits_remaining")
        if rem is None:
            return False, "Rainforest account unreachable"
        if rem <= 0:
            return False, "Rainforest is out of credits"
        return True, f"Rainforest OK ({rem} credits left)"
    except Exception as e:
        return False, f"Rainforest unreachable ({str(e)[:50]})"


def _bettercontact() -> tuple[bool, str]:
    key = os.environ.get("BETTERCONTACT_API_KEY")
    if not key:
        return False, "BetterContact has no API key set"
    try:
        import bettercontact_sync as bc
        bc.enrich_contacts(_BC_PROBE, key, timeout_s=BC_PROBE_TIMEOUT_S)
        return True, "BetterContact OK"
    except Exception as e:
        return False, f"BetterContact enrichment not responding ({str(e)[:60]})"


def check(revenue_first: bool = False, use_cache: bool = True) -> tuple[bool, list[str]]:
    """Return (all_healthy, [per-provider status lines]) for the providers this
    batch needs: BetterContact always; Rainforest too when revenue_first."""
    probes = []
    if revenue_first:
        probes.append(("rainforest", _rainforest))
    probes.append(("bettercontact", _bettercontact))
    ok_all, msgs = True, []
    for name, fn in probes:
        ok, msg = _cached(name, fn) if use_cache else fn()
        msgs.append(msg)
        ok_all = ok_all and ok
    return ok_all, msgs


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--revenue-first", action="store_true")
    a = ap.parse_args()
    ok, msgs = check(revenue_first=a.revenue_first, use_cache=False)
    print("HEALTHY" if ok else "NOT HEALTHY")
    for m in msgs:
        print("  -", m)
