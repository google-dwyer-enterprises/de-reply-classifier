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
# SINGLE-contact probe, generous timeout. Measured 2026-07-12: a 1-contact
# enrich terminates reliably in ~40s, but a 3-contact batch didn't finish in
# 200s — BetterContact's MULTI-contact enrich is intermittently slow (the known
# flakiness the drain retries around). So a multi-contact probe FALSE-negatives
# on a healthy endpoint, which would wrongly make the tier-3 drain skip and
# stall queued batches. One contact is the right liveness signal: "can BC
# process an enrich at all?" — the drain's own chunk timeouts + retries handle
# the per-batch slowness. Costs ~1 credit per probe (cached PROBE_TTL_S).
BC_PROBE_TIMEOUT_S = 120   # a single-contact enrich that can't finish in 120s = degraded
_BC_PROBE = [
    {"first_name": "John", "last_name": "Smith", "company_domain": "nike.com", "company_name": "Nike"},
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


def _anthropic() -> tuple[bool, str]:
    """Both flows gate every company through a Haiku ICP judgment (and revenue-
    first also brand-verifies via Haiku). If Anthropic is down/rate-limited/out
    of credit, the ICP gate fails closed and the whole run yields nothing — so
    don't start. A 1-token ping is effectively free."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False, "Anthropic has no API key set"
    try:
        import anthropic
        anthropic.Anthropic(timeout=20).messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=1,
            messages=[{"role": "user", "content": "ping"}])
        return True, "Anthropic OK"
    except Exception as e:
        return False, f"Anthropic not responding ({str(e)[:60]})"


def bettercontact_ok(use_cache: bool = True) -> tuple[bool, str]:
    """Is BetterContact's enrich endpoint responsive right now? Used by the
    tier-3 drain to decide whether to attempt enrichment this poll (a degraded
    BC leaves queued rows pending rather than hanging the drain)."""
    return _cached("bettercontact", _bettercontact) if use_cache else _bettercontact()


def check(revenue_first: bool = False, use_cache: bool = True) -> tuple[bool, list[str]]:
    """Return (all_healthy, [per-provider status lines]) for the providers a
    batch must have UP to START spending.

    Classic flow: Anthropic (ICP gate) + BetterContact (inline enrichment).
    Revenue-first (tier-3): Anthropic + Rainforest (the revenue gate). NOT
    BetterContact — enrichment is decoupled into the drain queue, so a degraded
    BC no longer blocks the gate (which spends only Rainforest/Anthropic); the
    drain retries the enrich when BC recovers. This is what lets a revenue-first
    batch make progress during a BC-enrich outage instead of being held."""
    probes = [("anthropic", _anthropic)]   # the ICP gate needs it in BOTH flows
    if revenue_first:
        probes.append(("rainforest", _rainforest))
    else:
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
