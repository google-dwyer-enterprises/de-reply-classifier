PROMPT_VERSION = "v3"
MODEL = "claude-haiku-4-5"
BATCH_SIZE = 25
BODY_CHAR_LIMIT = 800
MAX_TOKENS = 2000

LABELS = [
    "booked",
    "interested",
    "interested_past",
    "not_now",
    "not_interested",
    "wrong_person",
    "no_longer_there",
    "customer_service",
    "unsubscribe",
    "oof",
    "other",
]

LABEL_SCORES: dict[str, int] = {
    "booked": 10,
    "interested": 5,
    "interested_past": 2,
    "no_longer_there": 2,
    "not_now": 0,
    "not_interested": 0,
    "customer_service": 0,
    "unsubscribe": 0,
    "oof": 0,
    "other": -5,
    "wrong_person": -10,
}

LABEL_DEFINITIONS = {
    "booked": "Meeting/call scheduled or explicit agreement to meet (calendar link sent/accepted, time proposed & confirmed).",
    "interested": "Positive signal wanting to continue the conversation now — asks for info, pricing, deck, or next step.",
    "interested_past": "Previously engaged positively in our outreach (asked questions, agreed to a call, etc.) but the conversation died. Excludes leads who chose a competitor — those go to not_interested.",
    "not_now": "Soft no tied to timing — busy, revisit next quarter, circle back later, budget-frozen, mid-project. Leaves the door open.",
    "not_interested": "Hard no with no timing qualifier — not a fit, no need, stop pitching, or already chose a competitor. Not angry, just declining.",
    "wrong_person": "Recipient is not the right contact — redirects to a colleague, says 'not my area', or forwards internally.",
    "no_longer_there": "Recipient has left the company / retired / role no longer exists. Distinct from wrong_person — they are gone, not redirecting.",
    "customer_service": "Reply is about an existing product/service issue, billing, support, or account question — not a sales response.",
    "unsubscribe": "Explicit request to be removed, stop emailing, opt out, remove from list, GDPR/CAN-SPAM language.",
    "oof": "Automated out-of-office reply from a SPECIFIC PERSON indicating they personally are away (vacation, maternity, parental leave, travel, sick). NOT generic helpdesk/support auto-replies acknowledging ticket receipt — those are customer_service.",
    "other": "Doesn't fit any label above, or reply is non-English and intent unclear, or message is empty/garbled, or promotional/marketing newsletter.",
}


EXCLUDED_ADDRESSES: set[str] = {
    "connect@epicglobalinc.com",
    "calendar-notification@google.com",
}

EXCLUDED_DOMAINS: set[str] = {
    "dwyer-enterprises.com",
    "sybill.ai",
    "mixmax.com",
    "calendly.com",
    "sophosemail.com",
}

EXCLUDED_LOCAL_PREFIXES: set[str] = {
    "do-not-reply",
    "donotreply",
    "no-reply",
    "noreply",
    "notifications",
    "calendar-notification",
    "mailer-daemon",
    "postmaster",
}


def is_excluded_sender(email: str | None) -> bool:
    if not email:
        return True
    e = email.strip().lower()
    if e in EXCLUDED_ADDRESSES:
        return True
    if "@" not in e:
        return False
    local, domain = e.rsplit("@", 1)
    if domain in EXCLUDED_DOMAINS:
        return True
    if local in EXCLUDED_LOCAL_PREFIXES:
        return True
    return False


def extract_client(campaign_name: str) -> str:
    """Temporary passthrough. Full CLIENT_MAP lands once prefixes are confirmed;
    historical rows get retrofitted via SQL update at that point."""
    if not campaign_name or campaign_name == "UNKNOWN":
        return "other"

    name = campaign_name.strip()

    if " | " in name:
        prefix = name.split(" | ")[0].strip()
        return prefix.split()[0] if prefix else "other"

    return name.split()[0] if name else "other"
