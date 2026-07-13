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
    """Gate on the BetterContact CREDIT BALANCE, not an enrich round-trip.

    Learned 2026-07-13: with <1 credit BC still accepts an enrich job (HTTP 201)
    and silently parks it 'on hold' forever instead of erroring — so an empty
    balance is indistinguishable from a hang, and an enrich-round-trip probe both
    (a) burns a credit when it works and (b) hangs for the full timeout when the
    balance is dead, giving no useful signal. The free /account balance is the
    right, fast, accurate check: if we can't complete one enrich, don't start.
    Also fires the proactive low-balance warning so it's caught before zero."""
    if not os.environ.get("BETTERCONTACT_API_KEY"):
        return False, "BetterContact has no API key set"
    try:
        import bettercontact_sync as bc
        credits = bc.account_credits()
        if credits is None:
            return False, "BetterContact account unreachable"
        try:                       # proactive 'running low' email (throttled)
            import credit_alerts
            credit_alerts.maybe_low_balance_alert("BetterContact", int(credits))
        except Exception:
            pass
        if credits < bc.BC_ACCOUNT_MIN_CREDITS:
            return False, (f"BetterContact is out of credits ({credits:g} left) — "
                           f"top up at app.bettercontact.rocks (enrich jobs park "
                           f"'on hold' until then)")
        return True, f"BetterContact OK ({credits:g} credits left)"
    except Exception as e:
        return False, f"BetterContact check failed ({str(e)[:60]})"


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

    Both flows: Anthropic (the ICP gate) + BetterContact CREDIT BALANCE (no
    point starting a batch that can only park its enrich jobs 'on hold' — the
    #1 real-world failure, and one that otherwise looks like a silent hang).
    Revenue-first additionally needs Rainforest (the revenue gate)."""
    probes = [("anthropic", _anthropic)]   # the ICP gate needs it in BOTH flows
    if revenue_first:
        probes.append(("rainforest", _rainforest))
    probes.append(("bettercontact", _bettercontact))   # credit balance, both flows
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
