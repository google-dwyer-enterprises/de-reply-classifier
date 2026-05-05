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
    """Return a psycopg2 connection. Caller must close()."""
    return psycopg2.connect(_build_dsn())


def refresh_lead_status() -> None:
    """Refresh the lead_status materialized view. Bypasses PostgREST timeout."""
    conn = connect()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("refresh materialized view concurrently lead_status_mv;")
    finally:
        conn.close()
