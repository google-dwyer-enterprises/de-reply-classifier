"""Shared utilities for the names-backfill pipeline.

Used by:
  - `backfill_names_from_replies.py` (production: extracts + writes)
  - `scripts/names_backfill/measure_option4.py` (measurement: read-only)

Anything related to the name-extraction pipeline (Option 4 architecture)
lives here so the production feature and the measurement script can't drift.

Pipeline (Option 4, post-hardening per BACKFILL_NAMES_FROM_REPLIES_PLAN.md):

  1. Technique A — email local-part heuristic ('john.smith@' → 'John'/'Smith').
  2. Technique C — Instantly GET /emails/{id} → from_address_json[0].name.
  3. Verification — every heuristic fill flows through:
       a) Deterministic fast-path: clean local-part match + no brand tokens
          + both first+last present → ACCEPT without LLM call.
       b) LLM verifier (Haiku 4.5) for the rest. Brand/role FP detection,
          uses pre-computed LOCAL_PART_MATCH signal.
  4. LLM extractor — runs on residual where heuristics didn't fill,
     reading body signatures.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from typing import Any

from anthropic import Anthropic

from instantly_sync import (
    API_BASE,
    request_with_backoff,
)


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

MODEL = "claude-haiku-4-5"
BATCH_SIZE = 25
MAX_TOKENS = 2000
BODY_MAX_CHARS = 2000

READ_CHUNK = 1000       # paginated reads from lead_contacts
LOOKUP_CHUNK = 100      # `.in_()` against leads — URL safety


# --------------------------------------------------------------------------- #
# Role-account filter
#
# Base canonical set + post-hardening extras (manager/resp/export/fba/etc.)
# Compound-rule: also split the local on [._-] and reject if any token
# is a role word. Catches `consumercare_usa`, `devaise-cs`, `resp.export`.
# --------------------------------------------------------------------------- #

ROLE_ACCOUNT_LOCALS: frozenset[str] = frozenset({
    # English
    "info", "support", "sales", "contact", "hello", "hi",
    "admin", "administrator",
    "billing", "accounts", "accounting", "ar", "ap",
    "hr", "it", "marketing",
    "office", "team",
    "help", "helpdesk",
    "service", "services", "customerservice", "customer-service", "customer",
    "ccare", "cs", "customercare",
    "webmaster", "postmaster", "webstore",
    "mail", "mailer",
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "enquiries", "inquiries",
    "careers", "jobs",
    "press", "media", "legal", "privacy",
    "shop", "orders", "order", "parts", "connect",
    # Multilingual
    "kundeservice",       # Danish "customer service"
    "atencion", "atc",    # Spanish "attention"
    "kundenservice",      # German "customer service"
    "servizioclienti",    # Italian
    "atendimento",        # Portuguese
})

ROLE_ACCOUNT_EXTRAS: frozenset[str] = frozenset({
    # Generic
    "manager", "mgr", "managers",
    "customersupport", "customer_support",
    "wholesale", "wholesales",
    "questions", "chat", "collaborations", "partnerships",
    "eshop", "e-shop", "ecommerce",
    "fba",                       # Amazon Fulfilled-By-Amazon seller mailbox
    # French
    "resp", "responsable", "export",
    "direction", "commerce", "commercial",
    "comptabilite", "accueil", "bonjour",
    "rgpd",                      # GDPR mailbox
    # German
    "vertrieb", "verkauf", "kontakt",
    # Spanish / Italian
    "gerencia", "direccion", "direzione", "commerciale",
})

ROLE_ACCOUNTS_FULL: frozenset[str] = ROLE_ACCOUNT_LOCALS | ROLE_ACCOUNT_EXTRAS

_TOKEN_SPLIT = re.compile(r"[._\-]+")
_TRAILING_DIGITS = re.compile(r"\d+$")


def is_role_account_email(email: str | None) -> bool:
    if not email or "@" not in email:
        return False
    local = email.split("@", 1)[0].strip().lower()
    if "+" in local:
        local = local.split("+", 1)[0]
    if local in ROLE_ACCOUNTS_FULL:
        return True
    for tok in _TOKEN_SPLIT.split(local):
        if not tok:
            continue
        tok = _TRAILING_DIGITS.sub("", tok)
        if tok and tok in ROLE_ACCOUNTS_FULL:
            return True
    return False


# --------------------------------------------------------------------------- #
# Name validators
# --------------------------------------------------------------------------- #

TITLE_FIRST_NAMES: frozenset[str] = frozenset({
    "dr", "mr", "mrs", "ms", "miss", "prof", "professor",
})

CREDENTIAL_LAST_NAMES: frozenset[str] = frozenset({
    "cpa", "mba", "md", "phd", "jd", "esq", "pe", "cfa", "pmp",
})


def validate_name(raw: object, kind: str) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s.lower() in ("null", "none"):
        return None
    if len(s) < 2:
        return None
    if s[0].isdigit():
        return None
    if "@" in s:
        return None
    norm = s.rstrip(".").lower()
    if norm in ROLE_ACCOUNT_LOCALS:
        return None
    if kind == "first" and norm in TITLE_FIRST_NAMES:
        return None
    if kind == "last" and norm in CREDENTIAL_LAST_NAMES:
        return None
    return s


# --------------------------------------------------------------------------- #
# Body cleaning — strip quoted threads, KEEP signatures
# --------------------------------------------------------------------------- #

_QUOTE_CUT_PATTERNS = [
    re.compile(r"^-{3,}\s*original message\s*-{3,}", re.I | re.M),
    re.compile(r"^-{5,}", re.M),
    re.compile(r"^On .+ wrote:\s*$", re.M),
    re.compile(r"^From:\s+.+$\s*(Reply-To:.+$\s*)?Date:.+$\s*To:.+$\s*Subject:.+$", re.M),
    re.compile(r"^From:\s+.+$\s*Sent:.+$\s*To:.+$\s*Subject:.+$", re.M),
    re.compile(r"^>>+", re.M),
]
_QUOTED_LINE_PATTERN = re.compile(r"^>+\s*")


def clean_for_extraction(body: str) -> str:
    """Strip quoted threads but keep signatures (where the sender's name lives)."""
    if not body:
        return ""
    cut = len(body)
    for pat in _QUOTE_CUT_PATTERNS:
        m = pat.search(body)
        if m:
            cut = min(cut, m.start())
    body = body[:cut]
    lines = [ln for ln in body.splitlines() if not _QUOTED_LINE_PATTERN.match(ln.lstrip())]
    body = "\n".join(lines)
    body = re.sub(r"[ \t]+", " ", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    if len(body) > BODY_MAX_CHARS:
        body = body[-BODY_MAX_CHARS:]
    return body


# --------------------------------------------------------------------------- #
# Name-field cleaner — applied to both first and last
#
# Strips trailing trailers (' from <Co>', ' - <Co>', ' | <Co>') and leading/
# trailing separators. Catches "Perkins from Rejuvaskin" → "Perkins",
# "- Soap Distillery" → None, "-Nina" → "Nina".
# --------------------------------------------------------------------------- #

_NAME_TRAILER_DELIMS: tuple[str, ...] = (
    " from ", " at ", " @ ", " - ", " — ", " – ", " | ", " / ",
)
_NAME_EDGE_SEPS = "-—– |/"


def clean_name_field(name: str | None) -> str | None:
    if not name:
        return None
    s = name
    lower = s.lower()
    for delim in _NAME_TRAILER_DELIMS:
        idx = lower.find(delim)
        if idx >= 0:
            s = s[:idx]
            lower = s.lower()
    s = s.strip().strip(_NAME_EDGE_SEPS).strip()
    return s or None


# --------------------------------------------------------------------------- #
# Brand-token check
#
# Used to gate the deterministic fast-path: even when the local-part matches
# the candidate name (e.g. offgrid@offgridknives.com + "Off-Grid Knives"),
# if either word contains a brand-like token, force the LLM verifier to weigh in.
# Token-level match plus a substring scan for ≥4-char tokens to catch
# concatenated compounds (Shopdump, Partysupplies, Thepreorder, etc.).
# --------------------------------------------------------------------------- #

BRAND_TOKENS: frozenset[str] = frozenset({
    # Company suffixes
    "inc", "llc", "ltd", "corp", "corporation", "company", "group",
    "gmbh", "ag", "bv", "pty", "plc", "limited",
    # Departments / roles
    "team", "support", "service", "services", "sales", "info", "contact",
    "rep", "admin", "official",
    # Locations as "names"
    "international", "global",
    # Business descriptors
    "store", "shop", "studio", "agency", "consulting", "industries", "industry",
    "labs", "tech", "designs", "design", "supplies", "supply",
    "boutique", "goods", "trading", "trade", "products", "solutions",
    "enterprises", "collection", "preorder", "essentials",
    # Brand-shape tokens observed in audits
    "knives", "kingdom", "byte", "playtime", "drop", "world",
    "hockey", "basketball", "football", "soccer",
    "fitness", "wellness", "beauty",
})


def has_brand_token(first: str | None, last: str | None) -> bool:
    """True if any token in first/last looks like a business word.
    Substring scan for ≥4-char tokens catches concatenated compounds."""
    for name in (first, last):
        if not name:
            continue
        n = name.lower()
        for tok in re.split(r"[\s\-'.]+", n):
            if tok and tok in BRAND_TOKENS:
                return True
        for bt in BRAND_TOKENS:
            if len(bt) >= 4 and bt in n:
                return True
    return False


# --------------------------------------------------------------------------- #
# Deterministic local-part ↔ name matcher
#
# Returns True only on clean, unambiguous patterns. Short locals (<3 chars)
# never deterministic-match; the LLM handles those.
# --------------------------------------------------------------------------- #

def local_matches_name(email: str, first: str | None, last: str | None) -> bool:
    if not email or "@" not in email:
        return False
    if not (first or last):
        return False
    local = email.split("@", 1)[0].lower()
    if "+" in local:
        local = local.split("+", 1)[0]
    local = _TRAILING_DIGITS.sub("", local)
    local_clean = re.sub(r"[._\-]", "", local)

    f = re.sub(r"[^a-z]", "", (first or "").lower())
    l = re.sub(r"[^a-z]", "", (last or "").lower())

    if not local_clean:
        return False

    # 1) Local == first or last alone
    if f and len(f) >= 3 and (local == f or local_clean == f):
        return True
    if l and len(l) >= 3 and (local == l or local_clean == l):
        return True

    if f and l and len(f) >= 2 and len(l) >= 2:
        # 2) Concatenations
        if local_clean in (f + l, l + f):
            return True
        # 3) Initial patterns
        fi, li = f[0], l[0]
        if local_clean in (fi + l, f + li, li + f, l + fi):
            return True
        # 4) Separator patterns on the raw local
        for sep in (".", "_", "-"):
            if local in (f + sep + l, l + sep + f, fi + sep + l, f + sep + li):
                return True
        # 5) Truncated-first + full-last (jabsoni = jab + soni)
        if len(f) >= 3 and len(l) >= 3:
            if local_clean.startswith(f) and local_clean.endswith(l):
                return True
            for k in range(3, len(f) + 1):
                if local_clean == f[:k] + l:
                    return True
    return False


# --------------------------------------------------------------------------- #
# Technique A — email local-part separator heuristic
# --------------------------------------------------------------------------- #

_COMPANY_TOKENS: frozenset[str] = frozenset({
    "team", "support", "service", "services", "sales", "info", "contact",
    "inc", "llc", "ltd", "co", "corp", "corporation", "company", "group",
    "gmbh", "ag", "sa", "bv", "pty", "plc",
    "official", "store", "shop", "studio", "agency", "consulting",
    "us", "usa", "uk", "eu",
    "rep", "admin",
})


def looks_like_personal_name(first: str | None, last: str | None) -> tuple[str | None, str | None]:
    """Filter out single-token company noise (e.g. 'Inc', 'LLC', 'NBP')."""
    def clean(tok: str | None) -> str | None:
        if not tok:
            return None
        if tok.lower() in _COMPANY_TOKENS:
            return None
        if len(tok) >= 3 and tok.isupper():
            return None
        if any(ch.isdigit() for ch in tok):
            return None
        return tok
    return (clean(first), clean(last))


_LOCALPART_DOT = re.compile(r"^([a-z]+)\.([a-z]+)$")
_LOCALPART_UNDERSCORE = re.compile(r"^([a-z]+)_([a-z]+)$")
_LOCALPART_DASH = re.compile(r"^([a-z]+)-([a-z]+)$")


def _localpart_to_name(local: str) -> tuple[str | None, str | None]:
    """Return (first, last). Conservative: only fires on `.`/`_`/`-` separators."""
    if not local:
        return (None, None)
    s = local.lower()
    if "+" in s:
        s = s.split("+", 1)[0]
    s = re.sub(r"\d+$", "", s)
    if not s or s in ROLE_ACCOUNT_LOCALS:
        return (None, None)
    if m := _LOCALPART_DOT.match(s):
        return (m.group(1).title(), m.group(2).title())
    if m := _LOCALPART_UNDERSCORE.match(s):
        return (m.group(1).title(), m.group(2).title())
    if m := _LOCALPART_DASH.match(s):
        return (m.group(1).title(), m.group(2).title())
    return (None, None)


def technique_a(email: str) -> tuple[str | None, str | None]:
    if not email or "@" not in email:
        return (None, None)
    local = email.split("@", 1)[0].strip().lower()
    raw_first, raw_last = _localpart_to_name(local)
    return (validate_name(raw_first, "first"), validate_name(raw_last, "last"))


# --------------------------------------------------------------------------- #
# Technique C — Instantly GET /emails/{id} → from_address_json[0].name
# --------------------------------------------------------------------------- #

def technique_c(session, limiter, message_id: str | None) -> tuple[str | None, str | None]:
    if not message_id:
        return (None, None)
    url = f"{API_BASE}/emails/{message_id}"
    try:
        resp = request_with_backoff(session, url, {}, limiter)
        item = resp.json()
    except Exception:
        return (None, None)
    if not isinstance(item, dict):
        return (None, None)
    from_json = item.get("from_address_json") or []
    if not isinstance(from_json, list) or not from_json:
        return (None, None)
    name = (from_json[0].get("name") or "").strip()
    if not name:
        return (None, None)
    parts = name.split()
    if len(parts) == 1:
        first, last = validate_name(parts[0], "first"), None
    else:
        first = validate_name(parts[0], "first")
        last = validate_name(" ".join(parts[1:]), "last")
    return looks_like_personal_name(first, last)


# --------------------------------------------------------------------------- #
# LLM prompts
# --------------------------------------------------------------------------- #

VERIFIER_SYSTEM_PROMPT = """You verify candidate names against email reply bodies for a sales CRM.

For each item you will see:
  - email: the sender's email address
  - candidate_first / candidate_last: a name pulled from email metadata (RFC 5322 display name OR a local-part heuristic). May be junk — a company/brand/product/team/role, not a person.
  - LOCAL_PART_MATCH: a pre-computed signal (yes/no) — whether the email's local-part is a clean match for the candidate name (exact first/last, concatenated full name, first-initial + last, etc.). This is computed by an external matcher; trust it.
  - body: the cleaned reply body (quoted threads stripped, signatures kept). May be empty.

Decide whether the candidate name is plausibly the actual human OWNER of this mailbox and SENDER of this reply.

RULES (apply in order):

R1. REJECT if the candidate name is clearly a company, brand, product, team, role, or department:
    "Cosmic Byte", "Potent Hockey", "Customer Service Team", "Poppy Playtime",
    "Off-Grid Knives", "Tech Designs LLC", "Consumer Affairs", "Artisan Home",
    "Eco Drop", "Basketball", "Kolor Kingdom", "Frida Customer Support", etc.
    Reject EVEN IF LOCAL_PART_MATCH is yes — brand-shaped locals are still brands.

R2. REJECT if the body is explicitly signed by a CLEARLY DIFFERENT individual
    (e.g. body signed "Best, Sarah" but candidate is "Tom Wilson"). A team/role
    sign-off ("The Acme Team") is NOT a different individual — see R4.

R3. ACCEPT if LOCAL_PART_MATCH is yes AND R1/R2 don't apply. The local-part
    match is strong, near-deterministic evidence that the candidate owns the
    mailbox. The body need not contain an explicit signature for accept —
    it just must not contradict via R2. Empty/sparse/auto-reply bodies are
    fine.

R4. If LOCAL_PART_MATCH is no:
    - ACCEPT if the body shows the candidate signing explicitly (signature,
      sign-off "Best, Jane Smith", OOO/auto-reply ending in the individual's
      name).
    - Otherwise REJECT — without local-part or body corroboration, the name
      is likely scraped from elsewhere and not the mailbox owner.

Output a JSON array, one object per input, in the same order:
[{"id": 1, "decision": "accept"}, {"id": 2, "decision": "reject"}]

Output ONLY the JSON array. No preamble, no code fences, no explanation."""


EXTRACTOR_SYSTEM_PROMPT = """You extract the sender's first and last name from email reply bodies for a sales CRM.

The sender is the person who wrote the reply — the one whose email address is shown in `email`. The body has already been cleaned of quoted threads, so you should NOT see our own outreach team's names.

For each reply, return the sender's name with these rules:
1. Look in the signature/sign-off (e.g. "Best, Pablo", "Thanks,\\nAric Gervelis", "Best regards,\\nJane Tang\\nPR Manager"). The signature is usually the last few non-empty lines.
2. If the body is an auto-responder signed by a TEAM, ROLE, or COMPANY ("Customer Service Team", "The Acme Team", "Support Team", "JSAUX Technical Support Team", "The Fullstar Support Team"), return null/null — there's no individual sender.
3. BUT: if the body is an auto-reply (e.g. "out of office", "on holiday", "currently on the road", "I am OOO") that ends with an individual person's signature, extract THAT individual's name. The auto-reply was sent by that person — they are the sender. Examples that SHOULD extract a name:
   - "I will be OOO until Friday. Reach out to Tim for urgent issues. — Rich Orstad" → first="Rich", last="Orstad"
   - "I am out of office until 1/6/26. Thanks! Brett Ozar" → first="Brett", last="Ozar"
   - "I'm currently on the road... Cheers, Tom" → first="Tom"
4. If only one name is visible (e.g. "Best, Pablo"), return that as first_name with last_name=null.
5. If a full name is visible (e.g. "Aric Gervelis"), return both.
6. Do NOT invent or guess names. Do NOT use the local-part of the email address as a name source. If you can't see a name in the body itself, return null/null.
7. The name must be the SENDER, not anyone they mention, quote, or redirect to (e.g. "Reach out to Tim Gearhart instead" — Tim is NOT the sender).

Output a JSON array, one object per input, in the same order:
[{"id": 1, "first_name": "Pablo", "last_name": null}, {"id": 2, "first_name": "Aric", "last_name": "Gervelis"}, {"id": 3, "first_name": null, "last_name": null}]

Output ONLY the JSON array. No preamble, no code fences, no explanation."""


def format_verifier_user(batch: list[dict]) -> str:
    """batch items: {email, candidate_first, candidate_last, local_match, body}"""
    lines = [f"Verify these {len(batch)} candidate names:", ""]
    for i, item in enumerate(batch, 1):
        body = (item["body"] or "").replace("\n", "\\n")
        if len(body) > 1500:
            body = body[-1500:]
        lines.append(f"[{i}] email={item['email']!r}")
        lines.append(f"    candidate_first={item['candidate_first']!r}  candidate_last={item['candidate_last']!r}")
        lines.append(f"    LOCAL_PART_MATCH: {'yes' if item['local_match'] else 'no'}")
        lines.append(f"    body=\"{body}\"")
        lines.append("")
    return "\n".join(lines)


def format_extractor_user(batch: list[dict]) -> str:
    """batch items: {email, body}"""
    lines = [f"Extract names from these {len(batch)} reply bodies:", ""]
    for i, item in enumerate(batch, 1):
        body = item["body"].replace("\n", "\\n")
        if len(body) > 1500:
            body = body[-1500:]
        lines.append(f"[{i}] email={item['email']!r}")
        lines.append(f"    body=\"{body}\"")
        lines.append("")
    return "\n".join(lines)


def parse_response(text: str) -> list[dict]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def call_haiku(anthropic: Anthropic, system: str, user: str) -> tuple[list[dict] | None, int, int, str | None]:
    """Returns (parsed_array_or_None, input_tokens, output_tokens, error_label_or_None)."""
    try:
        resp = anthropic.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except Exception as exc:
        return (None, 0, 0, f"api:{type(exc).__name__}:{exc}")
    in_tok = getattr(getattr(resp, "usage", None), "input_tokens", 0) or 0
    out_tok = getattr(getattr(resp, "usage", None), "output_tokens", 0) or 0
    try:
        parsed = parse_response(resp.content[0].text)
    except Exception as exc:
        return (None, in_tok, out_tok, f"parse:{type(exc).__name__}:{exc}")
    return (parsed, in_tok, out_tok, None)


# --------------------------------------------------------------------------- #
# Supabase utilities
# --------------------------------------------------------------------------- #

def _supabase_retry(fn, attempts: int = 5, label: str = "supabase"):
    """Retry a Supabase call with exponential backoff. Long paginated runs
    occasionally hit a read timeout; one retry is usually enough."""
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            sleep_s = 2 ** i
            print(f"    {label} retry {i + 1}/{attempts} after {type(exc).__name__}: {exc}; sleeping {sleep_s}s")
            time.sleep(sleep_s)
    raise last_exc  # type: ignore[misc]


def fetch_gap_emails(supabase) -> list[str]:
    """Paginate every lead_contacts row missing first or last name."""
    out: list[str] = []
    offset = 0
    while True:
        resp = _supabase_retry(
            lambda: supabase.table("lead_contacts")
            .select("lead_email")
            .or_("first_name.is.null,last_name.is.null")
            .range(offset, offset + READ_CHUNK - 1)
            .execute(),
            label=f"lead_contacts.range({offset})",
        )
        rows = resp.data or []
        out.extend((r["lead_email"] or "").lower() for r in rows if r.get("lead_email"))
        if len(rows) < READ_CHUNK:
            return out
        offset += READ_CHUNK
        if offset % 10_000 == 0:
            print(f"    fetched {offset} so far...")


def filter_visible_in_leads(supabase, emails: list[str]) -> list[str]:
    """Intersect with `leads`. Returns raw rows (may include duplicates)."""
    visible: list[str] = []
    for i in range(0, len(emails), LOOKUP_CHUNK):
        chunk = emails[i:i + LOOKUP_CHUNK]
        rows = _supabase_retry(
            lambda: supabase.table("leads").select("lead_email").in_("lead_email", chunk).execute().data or [],
            label=f"leads.in_(chunk@{i})",
        )
        visible.extend((r["lead_email"] or "").lower() for r in rows if r.get("lead_email"))
    return visible


def fetch_latest_replies_with_message_id(supabase, emails: list[str]) -> dict[str, dict]:
    """{email: {body, subject, reply_timestamp, instantly_message_id}} — latest reply per candidate."""
    out: dict[str, dict] = {}
    for i in range(0, len(emails), LOOKUP_CHUNK):
        chunk = emails[i:i + LOOKUP_CHUNK]
        rows = _supabase_retry(
            lambda: supabase.table("replies")
            .select("lead_email, subject, body, reply_timestamp, instantly_message_id")
            .in_("lead_email", chunk)
            .order("reply_timestamp", desc=True)
            .execute()
            .data or [],
            label=f"replies.in_(chunk@{i})",
        )
        for r in rows:
            em = (r.get("lead_email") or "").lower()
            if em and em not in out:
                out[em] = r
        if (i // LOOKUP_CHUNK) % 5 == 0:
            print(f"    fetched {min(i + LOOKUP_CHUNK, len(emails))}/{len(emails)}; matched {len(out)}")
    return out


# --------------------------------------------------------------------------- #
# Pipeline orchestrator
#
# Runs steps 1–7 of the Option 4 pipeline against the live data and returns
# (candidates, state, stats). Callers either format a measurement report
# (measure_option4.py) or build update payloads (backfill_names_from_replies.py)
# off the state dict.
#
# Per-candidate state shape:
#   {
#     "heuristic_origin": "A" | "C" | None,
#     "heuristic_first": str | None,
#     "heuristic_last":  str | None,
#     "verifier_path":   "fast_accept" | "llm_accept" | "llm_reject" |
#                        "no_match_no_body" | "batch_failed:<msg>" | None,
#     "source":          "A_fast" | "A_llm" | "C_fast" | "C_llm" |
#                        "LLM_extracted" | "unfilled_no_reply" |
#                        "unfilled_empty_body" | "unfilled_llm_null" |
#                        "unfilled_llm_filtered" | "unfilled_llm_failed",
#     "first":           str | None,   # final value to write (None if unfilled)
#     "last":            str | None,
#   }
#
# stats dict has funnel counters + token usage.
# --------------------------------------------------------------------------- #

def run_pipeline(
    supabase,
    session,
    limiter,
    anthropic: Anthropic,
    *,
    limit: int | None = None,
    skip_c: bool = False,
    skip_extractor: bool = False,
) -> tuple[list[str], dict[str, dict], dict]:
    """Run the Option 4 name-extraction pipeline. Read-only — no writes.

    Returns (candidates, state, stats).
    """
    # --- Pool ------------------------------------------------------------- #
    print("Step 1/7: pulling gap emails from lead_contacts...")
    gap = fetch_gap_emails(supabase)
    print(f"  {len(gap):,} gap rows.")

    print("\nStep 2/7: filtering to those visible in leads + deduping...")
    visible_raw = filter_visible_in_leads(supabase, gap)
    visible = list(dict.fromkeys(visible_raw))
    print(f"  {len(visible_raw):,} visible rows in leads ({len(visible):,} unique emails).")

    candidates = [em for em in visible if not is_role_account_email(em)]
    skipped_role = len(visible) - len(candidates)
    print(f"  {skipped_role:,} skipped (role-account local-part).")
    print(f"  {len(candidates):,} unique candidates.")

    if limit is not None:
        candidates = candidates[:limit]
        print(f"  --limit applied: capped to {len(candidates):,}")

    pool = len(candidates)
    if not pool:
        return [], {}, {"pool": 0}

    # --- Replies ---------------------------------------------------------- #
    print(f"\nStep 3/7: fetching latest reply (with message_id) for each candidate...")
    replies = fetch_latest_replies_with_message_id(supabase, candidates)
    have_reply = {em for em in candidates if em in replies}
    print(f"  {len(have_reply):,} candidates have a reply, {pool - len(have_reply):,} don't.")

    cleaned_body: dict[str, str] = {}
    for em in have_reply:
        cleaned_body[em] = clean_for_extraction(replies[em].get("body") or "")
    empty_body = {em for em in have_reply if len(cleaned_body[em]) < 5}
    print(f"  {len(empty_body):,} of those have empty body after cleaning.")

    state: dict[str, dict] = {em: {
        "heuristic_origin": None,
        "heuristic_first": None,
        "heuristic_last": None,
        "verifier_path": None,
        "source": None,
        "first": None,
        "last": None,
    } for em in candidates}

    # --- Step 4: Technique A --------------------------------------------- #
    print("\nStep 4/7: technique A (local-part heuristic) on all candidates...")
    a_fills = 0
    for em in candidates:
        f, l = technique_a(em)
        f = clean_name_field(f)
        l = clean_name_field(l)
        if f or l:
            state[em].update(heuristic_origin="A", heuristic_first=f, heuristic_last=l)
            a_fills += 1
    print(f"  A produced a heuristic fill on: {a_fills}/{pool}")

    # --- Step 5: Technique C --------------------------------------------- #
    c_raw_fills = 0
    c_attempted = 0
    no_message_id = 0
    if skip_c:
        print("\nStep 5/7: technique C SKIPPED (skip_c=True)")
    else:
        c_eligible = [em for em in candidates
                      if state[em]["heuristic_origin"] is None and em in have_reply]
        print(f"\nStep 5/7: technique C on A-misses with replies ({len(c_eligible)} candidates)...")
        t0 = time.time()
        for idx, em in enumerate(c_eligible, 1):
            mid = replies[em].get("instantly_message_id")
            if not mid:
                no_message_id += 1
                continue
            c_attempted += 1
            f, l = technique_c(session, limiter, mid)
            f = clean_name_field(f)
            l = clean_name_field(l)
            if f or l:
                state[em].update(heuristic_origin="C", heuristic_first=f, heuristic_last=l)
                c_raw_fills += 1
            if idx % 50 == 0 or idx == len(c_eligible):
                elapsed = time.time() - t0
                rate = idx / max(elapsed, 0.001)
                print(f"    {idx}/{len(c_eligible)}  "
                      f"({rate:.1f} req/s, {elapsed:.0f}s elapsed, C raw fills: {c_raw_fills})")
        print(f"  C raw fills: {c_raw_fills}/{c_attempted} ({no_message_id} skipped — no message_id)")

    # --- Step 6: Verification (fast-path + LLM) -------------------------- #
    print("\nStep 6/7: verifying heuristic fills (fast-path + LLM)...")
    fast_accept_A = 0
    fast_accept_C = 0
    fast_brand_blocked = 0
    no_match_no_body = 0
    items_for_llm: list[dict] = []
    for em in candidates:
        origin = state[em]["heuristic_origin"]
        if origin is None:
            continue
        hf, hl = state[em]["heuristic_first"], state[em]["heuristic_last"]
        match = local_matches_name(em, hf, hl)
        brand = has_brand_token(hf, hl)
        both = bool(hf and hl)
        body = cleaned_body.get(em, "")
        has_body = len(body) >= 5

        if match and both and not brand:
            state[em].update(
                verifier_path="fast_accept",
                source=f"{origin}_fast",
                first=hf, last=hl,
            )
            if origin == "A":
                fast_accept_A += 1
            else:
                fast_accept_C += 1
            continue

        if not has_body and not match:
            state[em]["verifier_path"] = "no_match_no_body"
            no_match_no_body += 1
            continue

        if brand and match:
            fast_brand_blocked += 1
        items_for_llm.append({
            "email": em,
            "origin": origin,
            "candidate_first": hf,
            "candidate_last": hl,
            "local_match": match,
            "body": body,
        })

    print(f"  Fast-path accepts: A={fast_accept_A}  C={fast_accept_C}  "
          f"(total {fast_accept_A + fast_accept_C})")
    print(f"  Brand-blocked from fast-path (must use LLM): {fast_brand_blocked}")
    print(f"  No-match + no-body (auto-routed to extractor or unfilled): {no_match_no_body}")
    print(f"  Sent to LLM verifier: {len(items_for_llm)}")

    verifier_in_tokens = 0
    verifier_out_tokens = 0
    verifier_accept_A = 0
    verifier_accept_C = 0
    verifier_reject = 0
    verifier_failed_batches = 0
    if items_for_llm:
        n_batches = (len(items_for_llm) + BATCH_SIZE - 1) // BATCH_SIZE
        for bi, batch in enumerate(chunked(items_for_llm, BATCH_SIZE), start=1):
            parsed, in_t, out_t, err = call_haiku(
                anthropic, VERIFIER_SYSTEM_PROMPT, format_verifier_user(batch)
            )
            verifier_in_tokens += in_t
            verifier_out_tokens += out_t
            if parsed is None:
                print(f"    verifier batch {bi}/{n_batches} failed: {err}")
                verifier_failed_batches += 1
                for item in batch:
                    state[item["email"]]["verifier_path"] = f"batch_failed:{err}"
                    verifier_reject += 1
                time.sleep(2)
                continue
            for i, item in enumerate(batch):
                em = item["email"]
                origin = item["origin"]
                decision = (parsed[i].get("decision") if i < len(parsed) else None) or "reject"
                decision = str(decision).strip().lower()
                if decision == "accept":
                    state[em].update(
                        verifier_path="llm_accept",
                        source=f"{origin}_llm",
                        first=item["candidate_first"],
                        last=item["candidate_last"],
                    )
                    if origin == "A":
                        verifier_accept_A += 1
                    else:
                        verifier_accept_C += 1
                else:
                    state[em]["verifier_path"] = "llm_reject"
                    verifier_reject += 1
            if bi % 4 == 0 or bi == n_batches:
                print(f"    batch {bi}/{n_batches}: accepts(A+C)={verifier_accept_A + verifier_accept_C} "
                      f"rejects={verifier_reject} failed_batches={verifier_failed_batches}")

    # --- Step 7: LLM extractor on residual ------------------------------- #
    extractor_in_tokens = 0
    extractor_out_tokens = 0
    extractor_successes = 0
    extractor_null = 0
    extractor_filtered = 0
    extractor_failed_batches = 0
    if skip_extractor:
        print("\nStep 7/7: residual extractor SKIPPED (skip_extractor=True)")
    else:
        residual_items: list[dict] = []
        for em in candidates:
            if state[em]["source"]:
                continue
            if em not in have_reply:
                state[em]["source"] = "unfilled_no_reply"
                continue
            body = cleaned_body.get(em, "")
            if len(body) < 5:
                state[em]["source"] = "unfilled_empty_body"
                continue
            residual_items.append({"email": em, "body": body})

        print(f"\nStep 7/7: LLM extractor on residual ({len(residual_items)} candidates, "
              f"batches of {BATCH_SIZE})...")
        n_batches = (len(residual_items) + BATCH_SIZE - 1) // BATCH_SIZE
        for bi, batch in enumerate(chunked(residual_items, BATCH_SIZE), start=1):
            parsed, in_t, out_t, err = call_haiku(
                anthropic, EXTRACTOR_SYSTEM_PROMPT, format_extractor_user(batch)
            )
            extractor_in_tokens += in_t
            extractor_out_tokens += out_t
            if parsed is None:
                print(f"    extractor batch {bi}/{n_batches} failed: {err}")
                extractor_failed_batches += 1
                for item in batch:
                    state[item["email"]]["source"] = "unfilled_llm_failed"
                time.sleep(2)
                continue
            for i, item in enumerate(batch):
                em = item["email"]
                if i >= len(parsed):
                    state[em]["source"] = "unfilled_llm_null"
                    extractor_null += 1
                    continue
                raw_first = parsed[i].get("first_name")
                raw_last = parsed[i].get("last_name")
                first = validate_name(raw_first, "first")
                last = validate_name(raw_last, "last")
                if (raw_first or raw_last) and not (first or last):
                    state[em]["source"] = "unfilled_llm_filtered"
                    extractor_filtered += 1
                elif first or last:
                    state[em].update(source="LLM_extracted", first=first, last=last)
                    extractor_successes += 1
                else:
                    state[em]["source"] = "unfilled_llm_null"
                    extractor_null += 1
            if bi % 4 == 0 or bi == n_batches:
                print(f"    batch {bi}/{n_batches}: extracted={extractor_successes} "
                      f"null={extractor_null} filtered={extractor_filtered}")

    # Catch-all for anything still uncategorized
    for em in candidates:
        if state[em]["source"] is None:
            state[em]["source"] = ("unfilled_no_reply" if em not in have_reply
                                   else "unfilled_empty_body")

    stats = {
        "pool": pool,
        "visible_raw": len(visible_raw),
        "visible_unique": len(visible),
        "skipped_role": skipped_role,
        "have_reply": len(have_reply),
        "empty_body": len(empty_body),
        "a_fills": a_fills,
        "c_raw_fills": c_raw_fills,
        "c_attempted": c_attempted,
        "no_message_id": no_message_id,
        "fast_accept_A": fast_accept_A,
        "fast_accept_C": fast_accept_C,
        "fast_brand_blocked": fast_brand_blocked,
        "no_match_no_body": no_match_no_body,
        "verifier_accept_A": verifier_accept_A,
        "verifier_accept_C": verifier_accept_C,
        "verifier_reject": verifier_reject,
        "verifier_failed_batches": verifier_failed_batches,
        "verifier_in_tokens": verifier_in_tokens,
        "verifier_out_tokens": verifier_out_tokens,
        "extractor_successes": extractor_successes,
        "extractor_null": extractor_null,
        "extractor_filtered": extractor_filtered,
        "extractor_failed_batches": extractor_failed_batches,
        "extractor_in_tokens": extractor_in_tokens,
        "extractor_out_tokens": extractor_out_tokens,
    }
    return candidates, state, stats


def cost_dollars(in_tokens: int, out_tokens: int) -> float:
    """Haiku 4.5 pricing: $1/M input, $5/M output."""
    return in_tokens / 1_000_000 * 1.0 + out_tokens / 1_000_000 * 5.0
