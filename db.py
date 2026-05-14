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


def connect():
    """Return a psycopg2 connection with statement_timeout + TCP keepalives.

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


def refresh_lead_status() -> None:
    """Refresh the lead_status materialized view. Bypasses PostgREST timeout."""
    conn = connect()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("refresh materialized view concurrently lead_status_mv;")
    finally:
        conn.close()
