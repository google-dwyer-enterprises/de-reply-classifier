"""Direct Postgres connection for ops that PostgREST can't handle (long refreshes,
COPY, etc.). Builds the connection string from SUPABASE_URL + SUPABASE_DB_PASSWORD.

Set SUPABASE_DB_URL in .env to override (e.g. to use the session pooler for
IPv4-only networks)."""

from __future__ import annotations

import os
import re
from urllib.parse import quote

import psycopg2
from dotenv import load_dotenv


def _build_dsn() -> str:
    load_dotenv()
    override = os.environ.get("SUPABASE_DB_URL", "").strip()
    if override:
        return override

    url = os.environ.get("SUPABASE_URL", "")
    pwd = os.environ.get("SUPABASE_DB_PASSWORD", "")
    if not url or not pwd:
        raise SystemExit(
            "Missing SUPABASE_URL or SUPABASE_DB_PASSWORD in .env "
            "(or set SUPABASE_DB_URL directly)"
        )
    m = re.search(r"https?://([^.]+)\.supabase\.co", url)
    if not m:
        raise SystemExit(f"Could not parse project ref from SUPABASE_URL={url}")
    project_ref = m.group(1)
    host = os.environ.get(
        "SUPABASE_DB_HOST", "aws-1-ap-northeast-1.pooler.supabase.com"
    ).strip()
    return (
        f"postgresql://postgres.{project_ref}:{quote(pwd, safe='')}"
        f"@{host}:5432/postgres?sslmode=require"
    )


def _new_connection():
    """A fresh real psycopg2 connection with statement_timeout + TCP keepalives.

    The Supabase pooler will drop idle/long-running connections silently,
    surfacing as 'SSL connection has been closed unexpectedly' mid-query.
    Setting a server-side statement_timeout makes the failure mode explicit,
    and TCP keepalives stop the pooler from dropping us during quiet stretches.

    statement_timeout is 5 min, matching the longest queries we run (the
    materialized-view refresh sets its own autocommit + bypass via
    refresh_lead_status, so this cap doesn't affect that path).
    """
    return psycopg2.connect(
        _build_dsn(),
        connect_timeout=10,
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=5,
        options="-c statement_timeout=300000",
    )


class _RequestConn:
    """Proxy over a real connection whose close() is a NO-OP, so the many DB
    helpers a single web request fans out to can share ONE connection instead
    of each opening its own (a /admin load was ~7 fresh TLS handshakes to the
    flaky pooler). Every helper's `finally: conn.close()` becomes a no-op; the
    real connection is closed once at request teardown (close_request_conn).
    Everything else — cursor(), commit(), rollback(), .closed, autocommit — is
    delegated to the real connection, so callers are unchanged. No __enter__/
    __exit__ because nothing uses the connection as a context manager (verified).
    """
    __slots__ = ("_real",)

    def __init__(self, real):
        object.__setattr__(self, "_real", real)

    def close(self):
        pass  # real close is deferred to request teardown

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_real"), name)

    def __setattr__(self, name, value):
        setattr(object.__getattribute__(self, "_real"), name, value)


def connect():
    """Return a psycopg2 connection (statement_timeout + TCP keepalives).

    Inside a Flask request this returns a SINGLE shared per-request connection
    (stored on flask.g, wrapped so .close() is a no-op) — so a page that hits
    several DB helpers opens ONE connection, not one per helper. The real
    connection is closed once at request teardown via close_request_conn().

    Outside a request (worker, cron, CLI) it returns a fresh real connection
    exactly as before — no behaviour change for non-web callers. Each request is
    handled by one thread/greenlet with its own flask.g, so the shared
    connection is never touched concurrently.
    """
    try:
        from flask import g, has_request_context
        if has_request_context():
            real = getattr(g, "_db_conn", None)
            if real is None or real.closed:
                real = _new_connection()
                g._db_conn = real
            return _RequestConn(real)
    except Exception:
        # Flask absent or not in a request context -> plain fresh connection.
        pass
    return _new_connection()


def close_request_conn(exc=None):
    """Flask teardown hook: roll back any dangling transaction and close the
    request-scoped connection. Safe no-op when no request connection was opened.
    """
    try:
        from flask import g
    except Exception:
        return
    real = getattr(g, "_db_conn", None)
    if real is None:
        return
    try:
        if not real.closed:
            try:
                real.rollback()   # clear any open/aborted tx before closing
            except Exception:
                pass
            real.close()
    finally:
        g._db_conn = None


def refresh_lead_status() -> None:
    """Refresh the lead_status materialized view. Bypasses PostgREST timeout."""
    conn = connect()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("refresh materialized view concurrently lead_status_mv;")
    finally:
        conn.close()


# Note: followup_tracker_mv / followup_messages_mv were converted from
# materialized views to regular views for NocoDB v2026 schema-sync
# compatibility (it doesn't auto-detect MVs). Regular views recompute on
# every query, so no refresh is needed — the old refresh helper was removed
# because `refresh materialized view ...` errors on a regular view.
