"""Gunicorn config for the Flask web service (app:app).

Auto-loaded by gunicorn when it's started from the repo root (Railway WORKDIR is
/app, and the image COPYs this file in). It only takes effect if the web
service's start command is a bare `gunicorn app:app --bind 0.0.0.0:$PORT`
(explicit CLI flags override these). If the start command hard-codes worker
flags, simplify it to the bare form so this file drives the config.

Why: the web app is I/O-bound (Postgres + external provider APIs). With a single
sync worker, ONE slow request blocks the entire site. gthread workers let
independent requests proceed while another waits on I/O.

Memory-safe default: workers=1 (one process -> no extra memory vs a single sync
worker) + threads=4 (concurrency without OOM risk, since I/O releases the GIL).
Scale workers UP only once the instance's memory headroom is confirmed, via the
WEB_CONCURRENCY env var — no code change needed. bind is intentionally NOT set
here; the Railway start command supplies --bind 0.0.0.0:$PORT.
"""
import os

workers = int(os.environ.get("WEB_CONCURRENCY", "1"))
threads = int(os.environ.get("WEB_THREADS", "4"))
worker_class = "gthread"
timeout = 60            # was default 30s; headroom for a slow DB/provider call
graceful_timeout = 30
keepalive = 5
