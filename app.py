"""Lead Reviewer — Flask app that replaces Jam's NocoDB review surface.

Three pages:
  GET  /                          → redirect to /batches
  GET  /submit                    → render submit form (HTTP Basic)
  POST /submit                    → insert scrape_requests row, redirect
  GET  /batches                   → list of recent batches (HTTP Basic)
  GET  /batch/<token>             → per-batch review (token-only)
  POST /batch/<token>/bulk-update → set N leads' lead_approval
  POST /batch/<token>/mass-approve → set parent scrape_requests.approval='approved'
  POST /batch/<token>/mass-reject  → set parent scrape_requests.approval='rejected'

Auth model:
  - HTTP Basic on /submit and /batches (env: LEAD_REVIEWER_USERNAME / _PASSWORD)
  - Random UUID token on /batch/<token> — same security model as NocoDB share URLs

The worker remains untouched. This app only:
  - inserts new scrape_requests rows (via /submit)
  - updates prospeo_new_leads.lead_approval (via /bulk-update)
  - updates scrape_requests.approval (via /mass-approve / mass-reject)

The Railway worker picks up everything else through its existing poll loop.
"""

from __future__ import annotations

import os
import sys
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from flask import (
    Flask, Response, abort, jsonify, redirect, render_template,
    request, url_for,
)
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db import connect


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Reuse the worker's industry list as the canonical set of options on the form.
# Avoids drift between what the form offers and what BC supports.
try:
    from bettercontact_sync import BC_INDUSTRIES
except Exception:
    BC_INDUSTRIES = [
        "Retail Apparel and Fashion", "Apparel Manufacturing", "Cosmetics",
        "Personal Care Product Manufacturing", "Food and Beverage Manufacturing",
        "Furniture and Home Furnishings Manufacturing",
        "Sporting Goods Manufacturing", "Consumer Goods", "Pet Services",
        "Retail Groceries", "Alternative Medicine",
        "Retail Health and Personal Care Products",
    ]

COUNTRIES = ["United States", "Canada"]

USERNAME = (os.environ.get("LEAD_REVIEWER_USERNAME") or "").strip()
PASSWORD = (os.environ.get("LEAD_REVIEWER_PASSWORD") or "").strip()


app = Flask(__name__)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _check_basic_auth(auth) -> bool:
    if not USERNAME or not PASSWORD:
        # Misconfigured deploy — fail closed so we don't accidentally expose.
        return False
    return bool(auth and auth.username == USERNAME and auth.password == PASSWORD)


def require_basic_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not _check_basic_auth(request.authorization):
            return Response(
                "Login required.",
                401,
                {"WWW-Authenticate": 'Basic realm="Lead Reviewer"'},
            )
        return fn(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _csv(values: list[str]) -> str:
    """Render a list of strings as the comma-separated text NocoDB-Multi
    Select uses in our schema."""
    return ",".join(v.strip() for v in values if v.strip())


def _parse_csv_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(s).strip() for s in value if str(s).strip()]
    return [s.strip() for s in str(value).split(",") if s.strip()]


def fetch_recent_batches(limit: int = 50) -> list[dict]:
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                select id, status, approval, scraped_count, moved_count,
                       credits_spent, created_at, ready_at, moved_at,
                       review_token, notes
                  from scrape_requests
                 order by id desc
                 limit %s
            """, (limit,))
            return list(cur.fetchall())
    finally:
        conn.close()


def fetch_batch_by_token(token: str) -> dict | None:
    # Validate the token shape before hitting Postgres — an unparseable token
    # would raise psycopg2.errors.InvalidTextRepresentation and bubble up as
    # a 500. We want a clean 404 instead, since invalid tokens are just
    # invalid lookups, not server errors.
    import uuid as _uuid
    try:
        _uuid.UUID(str(token))
    except (ValueError, TypeError, AttributeError):
        return None

    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                select id, status, approval, scraped_count, moved_count,
                       credits_spent, created_at, ready_at, moved_at,
                       review_token, notes,
                       industries, skip_industries, countries
                  from scrape_requests
                 where review_token = %s
            """, (token,))
            return cur.fetchone()
    finally:
        conn.close()


def fetch_leads_for_batch(scrape_request_id: int) -> list[dict]:
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                select id, email, first_name, last_name, title, company_name,
                       source_industry, lead_approval, lead_moved_at, rejected
                  from prospeo_new_leads
                 where scrape_request_id = %s
                 order by id
            """, (scrape_request_id,))
            return list(cur.fetchall())
    finally:
        conn.close()


def insert_scrape_request(
    requested_leads: int, industries: list[str], skip_industries: list[str],
    countries: list[str], notes: str,
) -> dict:
    """Insert a new scrape_requests row at status='pending'. Returns the
    row (incl. the auto-generated review_token)."""
    conn = connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                insert into scrape_requests
                  (requested_leads, industries, skip_industries, countries, notes)
                values (%s, %s, %s, %s, %s)
                returning id, review_token
            """, (
                requested_leads,
                _csv(industries),
                _csv(skip_industries),
                _csv(countries),
                notes or None,
            ))
            return cur.fetchone()
    finally:
        conn.commit()
        conn.close()


def bulk_update_lead_approval(
    scrape_request_id: int, lead_ids: list[int], status: str,
) -> int:
    """Set lead_approval on a specific list of lead ids belonging to a batch.

    Defensive: filters by scrape_request_id too so a leaked token can only
    affect that batch's rows. Returns the number of rows updated.
    """
    assert status in ("pending", "approved", "rejected"), f"invalid status {status}"
    if not lead_ids:
        return 0
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                update prospeo_new_leads
                   set lead_approval = %s
                 where scrape_request_id = %s
                   and id = any(%s)
            """, (status, scrape_request_id, lead_ids))
            return cur.rowcount
    finally:
        conn.commit()
        conn.close()


def set_batch_approval(scrape_request_id: int, value: str) -> None:
    assert value in ("approved", "rejected"), f"invalid value {value}"
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "update scrape_requests set approval = %s where id = %s",
                (value, scrape_request_id),
            )
    finally:
        conn.commit()
        conn.close()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return redirect(url_for("batches"))


@app.route("/healthz")
def healthz():
    """Railway / monitoring probe — checks DB reachability."""
    try:
        conn = connect()
        with conn.cursor() as cur:
            cur.execute("select 1")
            cur.fetchone()
        conn.close()
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


@app.route("/submit", methods=["GET"])
@require_basic_auth
def submit_form():
    return render_template(
        "submit.html",
        industries=BC_INDUSTRIES,
        countries=COUNTRIES,
    )


@app.route("/submit", methods=["POST"])
@require_basic_auth
def submit_post():
    try:
        requested_leads = int(request.form.get("requested_leads") or 0)
    except (TypeError, ValueError):
        requested_leads = 0
    if not (1 <= requested_leads <= 5000):
        return render_template(
            "submit.html",
            industries=BC_INDUSTRIES,
            countries=COUNTRIES,
            error="Requested leads must be between 1 and 5000.",
            form=request.form,
        ), 400

    industries = request.form.getlist("industries")
    skip_industries = request.form.getlist("skip_industries")
    countries = request.form.getlist("countries") or COUNTRIES.copy()
    notes = (request.form.get("notes") or "").strip()

    row = insert_scrape_request(
        requested_leads, industries, skip_industries, countries, notes,
    )
    return redirect(url_for("batches", submitted=row["id"]))


@app.route("/batches")
@require_basic_auth
def batches():
    submitted = request.args.get("submitted", type=int)
    rows = fetch_recent_batches()
    return render_template("batches.html",
                           rows=rows, just_submitted=submitted)


@app.route("/batch/<token>")
def batch_review(token):
    batch = fetch_batch_by_token(token)
    if not batch:
        abort(404)
    leads = fetch_leads_for_batch(batch["id"])
    counts = {
        "pending":  sum(1 for ld in leads if ld["lead_approval"] == "pending"),
        "approved": sum(1 for ld in leads if ld["lead_approval"] == "approved"),
        "rejected": sum(1 for ld in leads if ld["lead_approval"] == "rejected"),
        "total":    len(leads),
        "moved":    sum(1 for ld in leads if ld["lead_moved_at"] is not None),
    }
    return render_template(
        "batch_review.html",
        batch=batch, leads=leads, counts=counts,
    )


@app.route("/batch/<token>/bulk-update", methods=["POST"])
def batch_bulk_update(token):
    batch = fetch_batch_by_token(token)
    if not batch:
        abort(404)
    payload = request.get_json(silent=True) or {}
    status = (payload.get("status") or "").strip()
    if status not in ("pending", "approved", "rejected"):
        return jsonify(ok=False, error="invalid status"), 400
    raw_ids = payload.get("lead_ids") or []
    try:
        lead_ids = [int(i) for i in raw_ids]
    except (TypeError, ValueError):
        return jsonify(ok=False, error="lead_ids must be ints"), 400
    if not lead_ids:
        return jsonify(ok=False, error="lead_ids is empty"), 400

    updated = bulk_update_lead_approval(batch["id"], lead_ids, status)
    return jsonify(ok=True, updated=updated, status=status)


@app.route("/batch/<token>/mass-approve", methods=["POST"])
def batch_mass_approve(token):
    batch = fetch_batch_by_token(token)
    if not batch:
        abort(404)
    set_batch_approval(batch["id"], "approved")
    return jsonify(ok=True)


@app.route("/batch/<token>/mass-reject", methods=["POST"])
def batch_mass_reject(token):
    batch = fetch_batch_by_token(token)
    if not batch:
        abort(404)
    set_batch_approval(batch["id"], "rejected")
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# Filters used in templates
# ---------------------------------------------------------------------------

@app.template_filter("csvlist")
def _filter_csvlist(s):
    return _parse_csv_list(s)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
