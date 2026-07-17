"""Gunicorn config for the Flask web service (app:app) — the single source of
truth for the web runtime. Dockerfile.web runs:

    gunicorn -c gunicorn.conf.py -b 0.0.0.0:$PORT app:app

The app is I/O-bound (Postgres + external provider APIs). It previously ran
2 *sync* workers, so a request held its whole worker while waiting on I/O and
two slow requests could freeze the site. gthread workers add threads so
independent requests proceed during that wait.

workers=2 keeps the prior capacity (the instance has ample memory); threads=4
adds I/O concurrency on top (2 x 4 = 8 concurrent requests). Both are tunable
via WEB_CONCURRENCY / WEB_THREADS env vars without a rebuild. bind is supplied
on the CLI (--bind 0.0.0.0:$PORT) because $PORT is only known at runtime.
"""
import os

workers = int(os.environ.get("WEB_CONCURRENCY", "2"))
threads = int(os.environ.get("WEB_THREADS", "4"))
worker_class = "gthread"
timeout = 120            # matches the prior Dockerfile.web setting
graceful_timeout = 30
keepalive = 5
accesslog = "-"          # -> stdout, so requests show in Railway logs
errorlog = "-"           # -> stderr
