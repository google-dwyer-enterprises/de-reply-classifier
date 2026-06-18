"""Email notifications for the lead-scrape automation worker.

Single function: `send_batch_ready_email(req)` posts to Resend's HTTP API.

Why Resend (not SMTP):
  - Single HTTP POST, no SMTP TLS / Gmail app-password fiddling
  - 3,000 emails/month free, paid plans cheap
  - DKIM/SPF handled once via a verified sending domain

Env vars required:
  RESEND_API_KEY        — from resend.com → API Keys
  NOTIFY_EMAIL          — where to send (e.g. jam@dwyer-enterprises.com)
  NOTIFY_FROM           — sender (defaults to "Dwyer Lead Scraper <noreply@dwyer-enterprises.com>")
  NOCODB_ROW_URL_TEMPLATE — expanded-row URL template with {id} placeholder.
                          Paste the URL of any expanded scrape_requests row from
                          your NocoDB browser tab, then replace the numeric row
                          id with the literal string {id}. Example:
                          https://nocodb.example.com/dashboard/#/nc/abc/def/?rowId={id}
  NOCODB_BASE_URL       — optional fallback if NOCODB_ROW_URL_TEMPLATE is unset;
                          email will just link to the NocoDB root.

If RESEND_API_KEY is unset, the function logs the email body and returns
without sending. That's useful for local development and means a missing key
never fails the job — the worker continues to record `status='ready'` so Jam
can still see the request in the NocoDB grid.
"""
from __future__ import annotations

import os
import sys
from typing import Any

import requests

RESEND_ENDPOINT = "https://api.resend.com/emails"
DEFAULT_FROM = "Dwyer Lead Scraper <noreply@dwyer-enterprises.com>"


def _build_link(req_id: int, review_token: str | None = None) -> str | None:
    """Build the "open this batch" link for the email.

    Preference order (most-scoped first):
      1. lead-reviewer per-batch URL — <LEAD_REVIEWER_BASE_URL>/batch/<review_token>.
         The lead-reviewer Flask app structurally scopes the page to this batch
         (route-based isolation, not filter-based). This is the production path.
      2. NOCODB_ROW_URL_TEMPLATE expanded with `{id}` — opens the parent
         scrape_requests row in NocoDB. Legacy fallback for the old NocoDB-only
         flow; only used if LEAD_REVIEWER_BASE_URL isn't set.
      3. NOCODB_BASE_URL — just the NocoDB root, manual navigation.

    Returns None when nothing is configured, so the caller renders a
    placeholder instead of a non-URL hint string that mail clients
    punycode-encode into garbage.
    """
    # 1. lead-reviewer per-batch URL (preferred)
    base = os.environ.get("LEAD_REVIEWER_BASE_URL", "").strip().rstrip("/")
    if base and review_token:
        return f"{base}/batch/{review_token}"

    template = os.environ.get("NOCODB_ROW_URL_TEMPLATE", "").strip()
    if template:
        if "{id}" in template:
            return template.replace("{id}", str(req_id))
        print(f"notifier: NOCODB_ROW_URL_TEMPLATE has no {{id}} placeholder; "
              f"appending ?rowId={req_id} as a guess", file=sys.stderr)
        sep = "&" if "?" in template else "?"
        return f"{template}{sep}rowId={req_id}"
    nocodb_base = os.environ.get("NOCODB_BASE_URL", "").rstrip("/")
    if nocodb_base:
        return nocodb_base
    return None


def _build_html(req: dict[str, Any]) -> str:
    """The HTML email body. Mirrors LEAD_AUTOMATION_MOCKUPS.html screen 3."""
    req_id = req["id"]
    requested = req["requested_leads"]
    scraped = req.get("scraped_count", 0)
    credits = req.get("credits_spent", 0)
    by_industry = req.get("by_industry") or []   # list of (industry, count) pairs

    industry_lines = "".join(
        f'<li style="margin:2px 0;">{name}: <strong>{n}</strong></li>'
        for name, n in by_industry[:5]
    ) or '<li style="color:#6b7280;">(none)</li>'

    link = _build_link(req_id, req.get("review_token"))
    # Label differs by which link target: the lead-reviewer is "Review batch",
    # the NocoDB row-template fallback is "Open request" — so Jam knows where
    # she's about to land.
    using_lead_reviewer = bool(
        req.get("review_token")
        and os.environ.get("LEAD_REVIEWER_BASE_URL", "").strip()
    )
    btn_label = (
        f'Review batch #{req_id} →'
        if using_lead_reviewer
        else f'Open request #{req_id} in NocoDB →'
    )
    if link:
        button = (
            f'<a href="{link}" '
            f'style="display:inline-block;background:#2563eb;color:white;'
            f'padding:12px 28px;border-radius:6px;text-decoration:none;'
            f'font-weight:600;margin:12px 0;">'
            f'{btn_label}</a>'
        )
    else:
        # Operator hasn't set NOCODB_ROW_URL_TEMPLATE / NOCODB_BASE_URL yet.
        # Don't render a fake link the mail client will mangle — show a hint.
        button = (
            '<span style="display:inline-block;background:#f3f4f6;color:#6b7280;'
            'padding:12px 28px;border-radius:6px;font-weight:600;margin:12px 0;'
            'border:1px dashed #d1d5db;">'
            f'Find request #{req_id} in your NocoDB scrape_requests grid '
            '(NocoDB link not configured)'
            '</span>'
        )

    return f"""\
<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;
            max-width:580px;margin:0 auto;padding:24px;color:#1a1a1a;
            line-height:1.6;">
  <p>Hi Jam,</p>
  <p>Batch <strong>#{req_id}</strong> finished scraping.
     <strong>{scraped} of {requested} requested leads</strong> were found
     (verified-deliverable decision makers).
     <strong>{credits} credits</strong> spent.</p>
  <p>Top industries this batch:</p>
  <ul>{industry_lines}</ul>
  <p>Open the request in NocoDB to review the preview and approve:</p>
  <p>{button}</p>
  <p>Once you set <strong>approval</strong> to <em>approved</em>, the leads
     will be loaded into the 200k contact pool automatically.</p>
  <p style="color:#6b7280;font-size:13px;margin-top:24px;">
     — Lead Scraper bot
  </p>
</div>
"""


def send_batch_ready_email(req: dict[str, Any]) -> bool:
    """Send the "batch ready" email for one request.

    Returns True on success, False on any failure (logged to stderr).
    A False return does NOT raise — the worker's contract is that email
    delivery is best-effort.

    `req` is expected to be a dict with at least:
      id (int), requested_leads (int), scraped_count (int),
      credits_spent (numeric), by_industry (list of (name, count) tuples).
    """
    api_key = (os.environ.get("RESEND_API_KEY") or "").strip()
    to = (os.environ.get("NOTIFY_EMAIL") or "").strip()
    sender = os.environ.get("NOTIFY_FROM", DEFAULT_FROM).strip()

    if not to:
        print("notifier: NOTIFY_EMAIL not set — skipping send", file=sys.stderr)
        return False
    if not api_key:
        # Dev mode: log instead of send.
        print(f"notifier: RESEND_API_KEY not set — would have sent to {to}",
              file=sys.stderr)
        print(_build_html(req)[:400], file=sys.stderr)
        return False

    subject = (f"Lead batch #{req['id']} ready for review — "
               f"{req.get('scraped_count', 0)} leads scraped")
    html = _build_html(req)

    payload = {
        "from": sender,
        "to": [to],
        "subject": subject,
        "html": html,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        r = requests.post(RESEND_ENDPOINT, json=payload,
                          headers=headers, timeout=30)
    except Exception as e:
        print(f"notifier: HTTP error sending to {to}: {e}", file=sys.stderr)
        return False

    if r.status_code >= 200 and r.status_code < 300:
        return True
    print(f"notifier: Resend returned {r.status_code}: {r.text[:200]}",
          file=sys.stderr)
    return False


def send_no_leads_email(req_id: int, reason: str) -> bool:
    """Tell Jam a batch finished but returned no leads, with the plain-English
    reason (e.g. budget too low). Not a crash — no 'engineer notified' wording."""
    api_key = (os.environ.get("RESEND_API_KEY") or "").strip()
    to = (os.environ.get("NOTIFY_EMAIL") or "").strip()
    sender = os.environ.get("NOTIFY_FROM", DEFAULT_FROM).strip()
    if not (api_key and to):
        return False

    subject = f"Lead batch #{req_id} — no leads scraped"
    link = _build_link(req_id)
    link_block = (
        f'<p>You can view the row in NocoDB: '
        f'<a href="{link}">Open request #{req_id}</a></p>'
        if link else
        f'<p>Find request #{req_id} in your NocoDB scrape_requests grid.</p>'
    )
    html = f"""\
<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:580px;
            margin:0 auto;padding:24px;line-height:1.6;">
  <p>Hi Jam,</p>
  <p>Batch <strong>#{req_id}</strong> finished but didn't return any leads.</p>
  <p style="background:#fffbeb;border:1px solid #fde68a;padding:12px;
            border-radius:6px;font-size:14px;">{reason[:600]}</p>
  <p>If this was a budget limit, raise the credit budget on the request and resubmit.</p>
  {link_block}
</div>
"""
    try:
        r = requests.post(
            RESEND_ENDPOINT,
            json={"from": sender, "to": [to], "subject": subject, "html": html},
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            timeout=30,
        )
        return 200 <= r.status_code < 300
    except Exception:
        return False


def send_failure_email(req_id: int, error: str) -> bool:
    """Optional: tell Jam a job failed. Used by worker on terminal errors."""
    api_key = (os.environ.get("RESEND_API_KEY") or "").strip()
    to = (os.environ.get("NOTIFY_EMAIL") or "").strip()
    sender = os.environ.get("NOTIFY_FROM", DEFAULT_FROM).strip()
    if not (api_key and to):
        return False

    subject = f"Lead batch #{req_id} FAILED"
    link = _build_link(req_id)
    link_block = (
        f'<p>You can view the row in NocoDB: '
        f'<a href="{link}">Open request #{req_id}</a></p>'
        if link else
        f'<p>Find request #{req_id} in your NocoDB scrape_requests grid '
        f'(NocoDB link not configured).</p>'
    )
    html = f"""\
<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:580px;
            margin:0 auto;padding:24px;line-height:1.6;">
  <p>Hi Jam,</p>
  <p>Batch <strong>#{req_id}</strong> failed before it could complete.</p>
  <pre style="background:#fef2f2;border:1px solid #fecaca;padding:12px;
              border-radius:6px;font-size:13px;overflow:auto;">{error[:600]}</pre>
  <p>Engineer's been notified by the logs.</p>
  {link_block}
</div>
"""
    try:
        r = requests.post(
            RESEND_ENDPOINT,
            json={"from": sender, "to": [to], "subject": subject, "html": html},
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            timeout=30,
        )
        return 200 <= r.status_code < 300
    except Exception:
        return False
