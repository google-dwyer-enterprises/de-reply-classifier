"""heartbeat.py — worker liveness signal.

The worker had no "am I alive?" signal: if the process died, batches just sat
`pending` and nothing showed it. The worker now beats once per poll AND the
scraper beats once per discovered page, so a long (multi-hour) batch never looks
dead. A heartbeat older than STALE_S means the worker process is down — surfaced
on the admin panel and by the prod-health skill.

Best-effort: never raises (a monitoring write must not break the worker).
"""
from __future__ import annotations

import os

STALE_S = 900   # 15 min without a beat => worker is down (pages beat every few min)


def beat() -> None:
    try:
        from db import connect
        conn = connect(); conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("""create table if not exists worker_heartbeat (
                             id int primary key, last_seen timestamptz not null, host text)""")
            cur.execute("""insert into worker_heartbeat (id, last_seen, host)
                           values (1, now(), %s)
                           on conflict (id) do update set last_seen = now(), host = excluded.host""",
                        ((os.environ.get("RAILWAY_SERVICE_NAME") or os.environ.get("HOSTNAME") or "")[:120] or None,))
        conn.close()
    except Exception:
        pass


def status() -> dict:
    """Return {last_seen, stale, host} for the admin panel / prod-health.
    stale is None if the store is unreachable (unknown, not 'down')."""
    try:
        from db import connect
        conn = connect()
        with conn.cursor() as cur:
            cur.execute("""create table if not exists worker_heartbeat (
                             id int primary key, last_seen timestamptz not null, host text)""")
            conn.commit()
            cur.execute("""select last_seen, host,
                                  (now() - last_seen) > make_interval(secs => %s)
                             from worker_heartbeat where id = 1""", (STALE_S,))
            r = cur.fetchone()
        conn.close()
        if not r:
            return {"last_seen": None, "host": None, "stale": True}   # never beat
        return {"last_seen": r[0], "host": r[1], "stale": bool(r[2])}
    except Exception:
        return {"last_seen": None, "host": None, "stale": None}
