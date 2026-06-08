"""Pre-export QA gate for scraped leads (BETTERCONTACT_LEAD_QUALITY_PLAN.md P5).

An independent, deterministic re-scan of the leads about to be exported. It does
NOT trust that the scrape-time filters ran (or ran with the current rules) — it
re-applies the prohibited-category and service-business checks over the full
signal set (name + domain + keywords + description) and:

  - BLOCKS the export if any prohibited-category lead (cannabis / alcohol /
    firearms) is present, or if the overall garbage rate exceeds a threshold.
  - Otherwise reports a clean bill of health.

This is the gate Jamie asked for ("no new leads until a QA process is in place")
and it doubles as the remediation path for leads accepted by an OLDER pipeline
version: `python run.py qa-leads --fix` quarantines every flagged row
(rejected=true) with an auditable reason, so a subsequent export is clean.

Reuses the exact matchers the scrape pipeline uses, so the gate and the filters
can never silently diverge.
"""

from __future__ import annotations

import json
from collections import Counter

from prospeo_sync import prohibited_category, service_business, is_allowlisted

# Above this fraction of flagged/total, refuse to export. Any prohibited lead at
# all (cannabis/alcohol/firearms) blocks regardless of rate — zero tolerance.
QA_GARBAGE_THRESHOLD = 0.005  # 0.5%


class QAGateError(Exception):
    """Raised when an export fails the QA gate."""


def _kw_blob(lead: dict) -> str:
    kw = lead.get("company_keywords")
    if isinstance(kw, (list, tuple)):
        return " ".join(str(k) for k in kw)
    return kw or ""


def scan(leads: list[dict]) -> dict:
    """Scan a list of lead dicts. Each may carry company_name, company_website,
    company_domain, company_description, company_keywords, email, provider.

    Returns a report dict:
        {
          total, flagged: [ {email, company_name, provider, category, token} ],
          prohibited_count, service_count,
          by_category: {category: n}, garbage_rate, threshold, passed
        }
    """
    flagged: list[dict] = []
    for ld in leads:
        name = ld.get("company_name")
        web = ld.get("company_website")
        dom = ld.get("company_domain")
        desc = ld.get("company_description")
        kb = _kw_blob(ld)

        # Allowlisted legit product brands are exempt from the deterministic
        # rules (the LLM brand-gate still classifies them at scrape time).
        if is_allowlisted(name, dom, web):
            continue

        # Prohibited categories: full signal set incl. description.
        ph = prohibited_category(name, web, dom, desc, kb)
        if ph:
            cat, token = ph
            flagged.append({
                "email": ld.get("email"), "company_name": name,
                "provider": ld.get("provider"),
                "category": f"prohibited:{cat}", "token": token,
            })
            continue

        # Service businesses: high-precision, name/domain/keywords only.
        sv = service_business(name, web, dom, kb)
        if sv:
            flagged.append({
                "email": ld.get("email"), "company_name": name,
                "provider": ld.get("provider"),
                "category": "service", "token": sv,
            })

    total = len(leads)
    prohibited_count = sum(1 for f in flagged if f["category"].startswith("prohibited"))
    service_count = sum(1 for f in flagged if f["category"] == "service")
    rate = (len(flagged) / total) if total else 0.0
    passed = (prohibited_count == 0) and (rate <= QA_GARBAGE_THRESHOLD)
    return {
        "total": total,
        "flagged": flagged,
        "prohibited_count": prohibited_count,
        "service_count": service_count,
        "by_category": dict(Counter(f["category"] for f in flagged)),
        "garbage_rate": rate,
        "threshold": QA_GARBAGE_THRESHOLD,
        "passed": passed,
    }


def scan_db_accepted(conn, *, provider: str | None = None,
                     mode: str | None = None) -> dict:
    """Scan every currently-accepted row in prospeo_new_leads, reconstructing the
    full signal set (BetterContact keywords/description come from
    bettercontact_raw; Prospeo rows fall back to name+domain only)."""
    sql = ["""
        select email, company_name, company_domain, company_website,
               provider, bettercontact_raw
        from prospeo_new_leads
        where not rejected
    """]
    params: list = []
    if provider:
        sql.append("and provider = %s")
        params.append(provider)
    if mode:
        sql.append("and scrape_mode = %s")
        params.append(mode)
    with conn.cursor() as cur:
        cur.execute(" ".join(sql), params)
        rows = cur.fetchall()

    leads: list[dict] = []
    for email, name, dom, web, prov, raw in rows:
        desc = None
        kw: list = []
        if raw:
            b = raw if isinstance(raw, dict) else json.loads(raw)
            if isinstance(b, dict):
                desc = b.get("company_description")
                kw = b.get("company_keywords") or []
        leads.append({
            "email": email, "company_name": name, "company_domain": dom,
            "company_website": web, "provider": prov or "prospeo",
            "company_description": desc, "company_keywords": kw,
        })
    return scan(leads)


def print_report(report: dict, *, sample: int = 12) -> None:
    """Human-readable QA report."""
    r = report
    verdict = "PASS" if r["passed"] else "FAIL"
    print(f"\n=== Lead QA gate: {verdict} ===")
    print(f"  scanned:          {r['total']}")
    print(f"  flagged:          {len(r['flagged'])} "
          f"({r['garbage_rate'] * 100:.2f}% vs {r['threshold'] * 100:.2f}% threshold)")
    print(f"    prohibited:     {r['prohibited_count']} "
          f"(cannabis/alcohol/firearms — zero-tolerance)")
    print(f"    service:        {r['service_count']}")
    if r["by_category"]:
        print("  by category:")
        for cat, n in sorted(r["by_category"].items(), key=lambda x: -x[1]):
            print(f"    {cat:<22} {n}")
    if r["flagged"]:
        print(f"  sample flagged (first {sample}):")
        for f in r["flagged"][:sample]:
            print(f"    [{f['category']:<18}] {str(f['company_name'])[:34]:<36} "
                  f"<{f['email']}> token={f['token']!r}")


def enforce(report: dict) -> None:
    """Raise QAGateError if the report did not pass."""
    if not report["passed"]:
        raise QAGateError(
            f"QA gate FAILED: {report['prohibited_count']} prohibited + "
            f"{report['service_count']} service among {report['total']} accepted "
            f"({report['garbage_rate'] * 100:.2f}% > "
            f"{report['threshold'] * 100:.2f}%). Run "
            f"`python run.py qa-leads --fix` to quarantine flagged rows, then "
            f"re-export. (Override with --force at your own risk.)"
        )


def fix_flagged(conn, report: dict) -> int:
    """Quarantine every flagged lead: set rejected=true with an auditable QA
    reason. Returns the number of rows updated. Idempotent."""
    flagged = report["flagged"]
    if not flagged:
        return 0
    rows = [
        (
            "prohibited" if f["category"].startswith("prohibited") else "service",
            f"QA gate: {f['category']} '{f['token']}'",
            f["email"],
        )
        for f in flagged if f.get("email")
    ]
    with conn.cursor() as cur:
        cur.executemany(
            "update prospeo_new_leads "
            "set rejected = true, agency_filter_result = %s, "
            "    agency_filter_method = 'qa_gate', agency_filter_reason = %s "
            "where email = %s and not rejected",
            rows,
        )
        n = cur.rowcount
    conn.commit()
    return n
