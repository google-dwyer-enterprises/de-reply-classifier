"""Credit-exhaustion email alerts — a reminder to renew a dead API key.

When a provider rejects a call because the account is out of credit / suspended
/ over quota, send a one-off reminder email (via Resend) so someone renews it —
instead of the failure surfacing only as a buried log line and a broken cron
(exactly what happened 2026-07-06 when the Anthropic balance ran dry and took
the whole daily refresh down).

Design:
  * `maybe_alert(provider, detail)` — call it from an `except`/error branch with
    the provider name + the error text. It (1) confirms the text looks like a
    credit/quota problem (not some unrelated error), (2) throttles to one email
    per provider per THROTTLE_HOURS (so retries/loops don't spam), (3) emails.
    Returns True if it decided this was a credit error (whether or not the mail
    actually sent — sending is best-effort and never raises).
  * `looks_like_credit_error(text)` — the pure matcher (unit-testable, no I/O).

Reuses the Resend setup from notifier.py (RESEND_API_KEY / NOTIFY_EMAIL /
NOTIFY_FROM). If those aren't set it logs and returns — never raises, so an
alert path can't itself break the pipeline.
"""
from __future__ import annotations

import os
import sys

import requests

RESEND_ENDPOINT = "https://api.resend.com/emails"
# Resend's universal sender works with NO verified domain — the right fallback
# for a safety-net alert, so a mis-set NOTIFY_FROM (or an unverified sending
# domain) can never be the reason the "your key is dead" email fails to send.
RESEND_FALLBACK_FROM = "Pipeline Monitor <onboarding@resend.dev>"
DEFAULT_FROM = "Dwyer Lead Scraper <noreply@dwyer-enterprises.com>"
THROTTLE_HOURS = 12
# Proactive low-balance warning: alert when a provider's REMAINING credits drop
# to/below this, BEFORE it hits 0, so it gets topped up before the pipeline
# stalls. (Distinct from maybe_alert, which is reactive — fires only once a call
# is already rejected for no credit.)
LOW_BALANCE_THRESHOLDS = {
    "Rainforest": 1000,      # ~10% of the 10k/mo Starter plan
    "BetterContact": 200,    # ~2-4 revenue-first batches of runway (≈1 cr/email)
}


def _resolve_sender() -> str:
    """A sender Resend will accept. NOTIFY_FROM must contain an '@' (a bare name
    like 'Hassan Mehmood' is rejected 422); if it doesn't, fall back to the
    always-valid resend.dev sender rather than failing the alert."""
    nf = (os.environ.get("NOTIFY_FROM") or "").strip()
    return nf if "@" in nf else RESEND_FALLBACK_FROM

# provider -> (list of lowercase signature substrings, where-to-renew hint).
# Substrings are matched case-insensitively against the error text.
SIGNATURES: dict[str, tuple[list[str], str]] = {
    "Anthropic": (["credit balance is too low", "insufficient credit",
                   "billing", "purchase credits"],
                  "console.anthropic.com → Plans & Billing"),
    "Rainforest": (["temporarily suspended", "out of credits", "out of api credits",
                    "credit balance", "quota", "subscribe to a plan"],
                   "app.rainforestapi.com → Account (top up / renew the plan)"),
    "OpenAI": (["insufficient_quota", "exceeded your current quota",
                "billing", "check your plan"],
               "platform.openai.com → Billing"),
    "Gemini": (["quota", "billing", "resource_exhausted"],
               "aistudio.google.com / Google Cloud console → Billing"),
    "BetterContact": (["insufficient credit", "not enough credit", "payment required",
                       "reports insufficient credits"],
                      "app.bettercontact.rocks → Billing"),
    "Prospeo": (["insufficient credit", "not enough credit", "quota", "payment required"],
                "prospeo.io → Billing"),
    "MillionVerifier": (["not enough credit", "insufficient credit", "low balance",
                         "too low"],
                        "millionverifier.com → Buy credits"),
    "Instantly": (["payment required", "plan limit", "upgrade your plan"],
                  "instantly.ai → Billing"),
}


def looks_like_credit_error(provider: str, text: str) -> bool:
    """True if `text` matches a known credit/quota-exhaustion signature for
    `provider`. Pure function — no I/O — so it's cheap to unit-test."""
    sig = SIGNATURES.get(provider)
    if not sig or not text:
        return False
    low = text.lower()
    return any(s in low for s in sig[0])


# --------------------------------------------------------------------------- #
# Throttle (one email per provider per THROTTLE_HOURS) — DB-backed, fail-open
# --------------------------------------------------------------------------- #
def _should_send(provider: str) -> bool:
    """Return True if we haven't alerted for this provider within the window.
    Records the send time on True. Fail-OPEN: if the state store is unreachable
    we still send (a duplicate reminder beats a silent dead key)."""
    try:
        from db import connect
        conn = connect(); conn.autocommit = True
        cur = conn.cursor()
        cur.execute("""create table if not exists credit_alert_state (
                         provider text primary key, last_sent_at timestamptz not null)""")
        cur.execute("""select last_sent_at > now() - make_interval(hours => %s)
                         from credit_alert_state where provider = %s""",
                    (THROTTLE_HOURS, provider))
        row = cur.fetchone()
        if row and row[0]:
            conn.close()
            return False   # alerted recently -> stay quiet
        cur.execute("""insert into credit_alert_state (provider, last_sent_at)
                       values (%s, now())
                       on conflict (provider) do update set last_sent_at = now()""",
                    (provider,))
        conn.close()
        return True
    except Exception as e:
        print(f"credit_alerts: throttle store unavailable ({e}); sending anyway",
              file=sys.stderr)
        return True


def _send_email(provider: str, detail: str, renew_hint: str) -> bool:
    api_key = (os.environ.get("RESEND_API_KEY") or "").strip()
    to = (os.environ.get("NOTIFY_EMAIL") or "").strip()
    sender = _resolve_sender()
    if not (api_key and to):
        print(f"credit_alerts: RESEND_API_KEY/NOTIFY_EMAIL not set — would alert "
              f"about {provider}: {detail[:120]}", file=sys.stderr)
        return False
    subject = f"⚠️ {provider} API out of credit — renew to keep the pipeline running"
    html = f"""\
<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:580px;
            margin:0 auto;padding:24px;line-height:1.6;color:#1a1a1a;">
  <p><strong>{provider}</strong> just rejected a request because the account is
     out of credit / suspended.</p>
  <p style="background:#fef2f2;border:1px solid #fecaca;padding:12px;border-radius:6px;
            font-size:13px;">{detail[:500]}</p>
  <p><strong>What to do:</strong> renew / top up at <strong>{renew_hint}</strong>.</p>
  <p style="color:#6b7280;font-size:13px;">Until then, steps that depend on {provider}
     will fail or be skipped. (You won't get another {provider} reminder for
     {THROTTLE_HOURS}h.)</p>
  <p style="color:#6b7280;font-size:13px;">— Pipeline monitor</p>
</div>"""
    try:
        r = requests.post(RESEND_ENDPOINT,
                          json={"from": sender, "to": [to], "subject": subject, "html": html},
                          headers={"Authorization": f"Bearer {api_key}",
                                   "Content-Type": "application/json"},
                          timeout=30)
        if 200 <= r.status_code < 300:
            print(f"credit_alerts: emailed {to} about {provider} credit exhaustion")
            return True
        print(f"credit_alerts: Resend {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"credit_alerts: send failed: {e}", file=sys.stderr)
        return False


def _send_low_balance_email(provider: str, remaining: int, threshold: int) -> bool:
    api_key = (os.environ.get("RESEND_API_KEY") or "").strip()
    to = (os.environ.get("NOTIFY_EMAIL") or "").strip()
    sender = _resolve_sender()
    renew = SIGNATURES.get(provider, ([], "the provider dashboard"))[1]
    if not (api_key and to):
        print(f"credit_alerts: RESEND_API_KEY/NOTIFY_EMAIL not set — would warn about "
              f"{provider} low balance ({remaining} left)", file=sys.stderr)
        return False
    subject = f"⚠️ {provider} credits running low — {remaining} left"
    html = f"""\
<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:580px;
            margin:0 auto;padding:24px;line-height:1.6;color:#1a1a1a;">
  <p><strong>{provider}</strong> credits are running low: <strong>{remaining}</strong>
     remaining (warning threshold {threshold}).</p>
  <p><strong>Top up before it hits 0</strong> at <strong>{renew}</strong> — once it
     runs out, revenue-first scraping stalls.</p>
  <p style="color:#6b7280;font-size:13px;">(You won't get another {provider}
     low-balance reminder for {THROTTLE_HOURS}h.)</p>
  <p style="color:#6b7280;font-size:13px;">— Pipeline monitor</p>
</div>"""
    try:
        r = requests.post(RESEND_ENDPOINT,
                          json={"from": sender, "to": [to], "subject": subject, "html": html},
                          headers={"Authorization": f"Bearer {api_key}",
                                   "Content-Type": "application/json"},
                          timeout=30)
        if 200 <= r.status_code < 300:
            print(f"credit_alerts: emailed {to} — {provider} low balance ({remaining})")
            return True
        print(f"credit_alerts: Resend {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"credit_alerts: low-balance send failed: {e}", file=sys.stderr)
        return False


def maybe_low_balance_alert(provider: str, remaining, threshold: int | None = None) -> bool:
    """Proactively warn when `provider` credits are LOW (before they hit 0).
    Throttled per provider (a key distinct from the exhaustion alert), records an
    api_event so it shows on the admin panel, and emails. Returns True if it
    fired (balance was at/under threshold). Never raises — safe to call on the
    hot path (it only touches the DB/email when actually low)."""
    try:
        if remaining is None:
            return False
        remaining = int(remaining)
        thr = threshold if threshold is not None else LOW_BALANCE_THRESHOLDS.get(provider)
        if thr is None or remaining > thr:
            return False   # cheap no-op in the normal (not-low) case
        detail = f"{provider} credits low: {remaining} remaining (threshold {thr})"
        try:   # surface on the admin panel (credit category)
            import api_events
            api_events.record(provider, "credit_exhausted", detail=detail, context="low_balance")
        except Exception:
            pass
        if _should_send(f"{provider}-lowbalance"):
            _send_low_balance_email(provider, remaining, thr)
        return True
    except Exception as e:
        print(f"credit_alerts: maybe_low_balance_alert error: {e}", file=sys.stderr)
        return False


def maybe_alert(provider: str, detail: str) -> bool:
    """If `detail` looks like a credit-exhaustion error for `provider`, send a
    throttled reminder email. Returns True if it was classified as a credit
    error (regardless of whether the email actually went out). Never raises."""
    try:
        if not looks_like_credit_error(provider, detail):
            return False
        renew_hint = SIGNATURES[provider][1]
        if _should_send(provider):
            _send_email(provider, detail, renew_hint)
        return True
    except Exception as e:
        print(f"credit_alerts: maybe_alert error: {e}", file=sys.stderr)
        return False


if __name__ == "__main__":
    # Manual test: `python credit_alerts.py "Anthropic"` sends a real test email.
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("provider", nargs="?", default="Anthropic")
    ap.add_argument("--detail", default="TEST — Your credit balance is too low to access the API.")
    ap.add_argument("--force", action="store_true", help="bypass the throttle")
    a = ap.parse_args()
    from dotenv import load_dotenv; load_dotenv()
    print("looks_like_credit_error:", looks_like_credit_error(a.provider, a.detail))
    if a.force:
        _send_email(a.provider, a.detail, SIGNATURES.get(a.provider, ([], "?"))[1])
    else:
        print("classified as credit error:", maybe_alert(a.provider, a.detail))
