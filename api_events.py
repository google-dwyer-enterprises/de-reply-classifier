"""api_events.py — record + read external-API problems for the admin panel.

Every rate-limit / timeout / overload / server-error / credit-exhaustion / auth
failure from a provider API (Anthropic, Rainforest, BetterContact, ...) is
logged to `api_event_log`, so the admin dashboard can show what's flaking, how
often, and when.

Best-effort by design: `record()` NEVER raises — a logging hiccup must not break
the call it's observing.
"""
from __future__ import annotations

import sys

# canonical problem kinds (what the admin panel groups by)
KINDS = ("rate_limit", "timeout", "overloaded", "server_error",
         "credit_exhausted", "auth", "other")


def ensure_schema(cur) -> None:
    cur.execute("""
        create table if not exists api_event_log (
            id          bigserial primary key,
            ts          timestamptz not null default now(),
            provider    text not null,
            kind        text not null,
            status_code int,
            context     text,
            detail      text
        )
    """)
    cur.execute("create index if not exists api_event_log_ts_idx on api_event_log (ts desc)")
    cur.execute("create index if not exists api_event_log_pk_idx on api_event_log (provider, kind)")


def classify_error(status_code: int | None, text: str | None) -> str:
    """Map an HTTP status + error text to one of KINDS."""
    low = (text or "").lower()
    # credit/quota exhaustion first (most actionable)
    if any(s in low for s in ("credit balance is too low", "insufficient credit",
                              "insufficient_quota", "exceeded your current quota",
                              "temporarily suspended", "out of credits",
                              "payment required", "subscribe to a plan")):
        return "credit_exhausted"
    if status_code == 429 or "rate limit" in low or "rate_limit" in low or "too many requests" in low:
        return "rate_limit"
    if status_code == 529 or "overloaded" in low:
        return "overloaded"
    if status_code in (408,) or "timeout" in low or "timed out" in low or "read timed out" in low:
        return "timeout"
    if status_code in (401, 403) or "unauthorized" in low or "forbidden" in low or "invalid api key" in low:
        return "auth"
    if status_code is not None and 500 <= status_code < 600:
        return "server_error"
    return "other"


def record(provider: str, kind: str, *, detail: str | None = None,
           status_code: int | None = None, context: str | None = None) -> None:
    """Insert one event. Swallows all errors (best-effort observability)."""
    try:
        from db import connect
        conn = connect(); conn.autocommit = True
        cur = conn.cursor()
        ensure_schema(cur)
        cur.execute(
            "insert into api_event_log (provider, kind, status_code, context, detail) "
            "values (%s,%s,%s,%s,%s)",
            (provider, kind if kind in KINDS else "other", status_code,
             (context or "")[:120] or None, (detail or "")[:1000] or None),
        )
        conn.close()
    except (Exception, SystemExit) as e:
        # Best-effort observability must NEVER break its caller. db.connect()
        # raises SystemExit (not Exception) when DB creds are absent — catch that
        # too, so a mis/unconfigured env (e.g. CI) can't turn a telemetry write
        # into an uncaught exception in the pipeline path that called us.
        print(f"api_events: could not record {provider}/{kind}: {e}", file=sys.stderr)


def record_error(provider: str, status_code: int | None, text: str | None,
                 context: str | None = None) -> str:
    """Convenience: classify then record. Returns the kind chosen."""
    kind = classify_error(status_code, text)
    record(provider, kind, detail=text, status_code=status_code, context=context)
    return kind


# --------------------------------------------------------------------------- #
# Read side (admin panel)
# --------------------------------------------------------------------------- #
def _rows(cur) -> list:
    return cur.fetchall()


def summary(hours: int = 24) -> dict:
    """Counts per (provider, kind) within the window + totals + provider totals."""
    import psycopg2.extras
    from db import connect
    conn = connect()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        ensure_schema(cur); conn.commit()
        cur.execute("""
            select provider, kind, count(*) n, max(ts) last_ts
              from api_event_log
             where ts > now() - make_interval(hours => %s)
             group by provider, kind
             order by n desc
        """, (hours,))
        by_pk = [dict(r) for r in cur.fetchall()]
        cur.execute("""
            select kind, count(*) n from api_event_log
             where ts > now() - make_interval(hours => %s) group by kind
        """, (hours,))
        by_kind = {r["kind"]: r["n"] for r in cur.fetchall()}
        cur.execute("select count(*) n from api_event_log where ts > now() - make_interval(hours => %s)", (hours,))
        total = cur.fetchone()["n"]
    finally:
        conn.close()
    return {"by_pk": by_pk, "by_kind": by_kind, "total": total, "hours": hours}


def recent(limit: int = 100) -> list[dict]:
    import psycopg2.extras
    from db import connect
    conn = connect()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        ensure_schema(cur); conn.commit()
        cur.execute("""
            select ts, provider, kind, status_code, context, detail
              from api_event_log order by ts desc limit %s
        """, (limit,))
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def credit_alert_state() -> list[dict]:
    """Last credit-exhaustion alert email sent per provider (from credit_alerts)."""
    import psycopg2.extras
    from db import connect
    conn = connect()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""select provider, last_sent_at from credit_alert_state
                       order by last_sent_at desc""")
        return [dict(r) for r in cur.fetchall()]
    except Exception:
        return []
    finally:
        conn.close()
