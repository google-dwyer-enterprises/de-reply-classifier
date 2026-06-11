"""MillionVerifier email verification (meeting tasks #8/#9, 2026-06-11).

Gate: approved leads are MV-verified AFTER human approval and BEFORE moving
into lead_contacts (the 200k pool). Only result='ok' moves; definitive
non-ok results (invalid/disposable/catch_all/unknown/unverified) flip the
lead back to rejected with the reason stamped, so the batch still finalizes
and the reviewer sees why. Transient API errors leave the lead untouched —
the worker's poll loop naturally retries the move next cycle.

API: GET https://api.millionverifier.com/api/v3?api=KEY&email=...&timeout=20
  -> {"result": "ok|invalid|unverified|catch_all|disposable|unknown",
      "credits": <remaining>, "error": "...", ...}
1 credit per verification. Demo key "API_KEY_FOR_TEST" returns random
results free (used by --demo smoke tests).
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import requests

MV_API_URL = "https://api.millionverifier.com/api/v3"
MV_TIMEOUT_S = 25            # MV-side verification timeout (their param max 60)
MV_WORKERS = 4
# Only a clean 'ok' enters the 200k pool. catch_all is deliberately excluded:
# BetterContact already pre-filtered to 'deliverable', so a catch_all here is
# a downgrade signal, not an unlucky retest.
MOVABLE_RESULTS = {"ok"}
DEFINITIVE_BAD = {"invalid", "disposable", "catch_all", "unknown", "unverified"}


def api_key() -> str | None:
    return (os.environ.get("MILLIONVERIFIER_API_KEY") or "").strip() or None


def enabled() -> bool:
    return api_key() is not None


def verify_email(email: str, key: str | None = None) -> dict:
    """Verify one email. Returns {'result': <str>, 'credits': <int|None>}.

    'result' is one of MV's values, or 'error' for transport/API failures
    (callers must treat 'error' as retry-later, never as a verdict).
    """
    key = key or api_key()
    if not key:
        return {"result": "error", "credits": None, "detail": "no api key"}
    try:
        r = requests.get(MV_API_URL,
                         params={"api": key, "email": email,
                                 "timeout": MV_TIMEOUT_S},
                         timeout=MV_TIMEOUT_S + 10)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        return {"result": "error", "credits": None,
                "detail": f"{type(e).__name__}: {e}"}
    if data.get("error"):
        # API-level error (bad key, out of credits, ...) — loud, retryable.
        return {"result": "error", "credits": data.get("credits"),
                "detail": str(data["error"])[:200]}
    result = (data.get("result") or "").strip().lower()
    if result not in MOVABLE_RESULTS | DEFINITIVE_BAD:
        return {"result": "error", "credits": data.get("credits"),
                "detail": f"unexpected result {result!r}"}
    return {"result": result, "credits": data.get("credits")}


def verify_emails(emails: list[str], key: str | None = None,
                  on_log=print) -> dict[str, dict]:
    """Verify a batch concurrently. Returns {email: verify_email-dict}."""
    key = key or api_key()
    out: dict[str, dict] = {}
    if not emails:
        return out

    def one(email: str) -> None:
        out[email] = verify_email(email, key)

    with ThreadPoolExecutor(min(MV_WORKERS, len(emails))) as ex:
        list(ex.map(one, emails))
    errors = sum(1 for v in out.values() if v["result"] == "error")
    if errors:
        on_log(f"  millionverifier: {errors}/{len(emails)} calls errored "
               f"(will retry next poll): "
               f"{next(v['detail'] for v in out.values() if v['result'] == 'error')}")
    credits = next((v["credits"] for v in out.values()
                    if v.get("credits") is not None), None)
    if credits is not None and credits < 1000:
        on_log(f"  millionverifier: LOW CREDITS — {credits} remaining")
    return out
