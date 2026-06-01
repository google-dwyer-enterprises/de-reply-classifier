"""v4 probe: now that GET /v2/emails?lead=<email> works, deep-inspect the
14 items returned for jkirsteinn@gmail.com — and re-test all 5 probe leads
with this filter to get per-lead coverage numbers.
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

API_BASE = "https://api.instantly.ai/api/v2"
LIST_EMAILS_URL = f"{API_BASE}/emails"
CSV_PATH = Path("original_data/followup_tracker_2026-05-19.csv")
DEBUG_DIR = Path("debug")


def pick_probes(n=5) -> list[dict]:
    with open(CSV_PATH, "r", encoding="utf-8", errors="replace") as f:
        rows = list(csv.reader(f))
    body_cols = [9, 11, 13, 15, 17, 19, 21, 23]
    eom_body_cols = [29, 31, 33, 35]
    candidates = []
    for r in rows[1:]:
        if len(r) < 36:
            continue
        if r[4].strip() != "Booked":
            continue
        email = r[1].strip().lower()
        if "\n" in email or "," in email or "@" not in email:
            continue
        bodies = sum(1 for c in body_cols if r[c].strip())
        eom = sum(1 for c in eom_body_cols if r[c].strip())
        if bodies + eom < 2:
            continue
        candidates.append({"email": email, "client": r[0],
                          "ffups": bodies, "eom": eom,
                          "init_reply": r[7][:120],
                          "ffup1": r[9][:120] if len(r) > 9 else ""})
    return candidates[:n]


def fetch_lead_emails(session, email, email_type=None) -> list[dict]:
    params = {"lead": email, "limit": 100}
    if email_type:
        params["email_type"] = email_type
    all_items: list[dict] = []
    cursor = None
    for _ in range(20):  # hard cap pagination
        if cursor:
            params["starting_after"] = cursor
        resp = session.get(LIST_EMAILS_URL, params=params, timeout=60)
        if resp.status_code != 200:
            print(f"    ! {resp.status_code}: {resp.text[:150]}")
            break
        payload = resp.json()
        items = payload.get("items") or []
        all_items.extend(items)
        nxt = payload.get("next_starting_after")
        if not nxt or nxt == cursor or len(items) < 100:
            break
        cursor = nxt
        time.sleep(0.5)
    return all_items


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    load_dotenv()
    api_key = os.environ.get("INSTANTLY_API_KEY", "").strip()
    if not api_key:
        sys.exit("FATAL: INSTANTLY_API_KEY not set")

    DEBUG_DIR.mkdir(exist_ok=True)
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {api_key}", "Accept": "application/json"})

    probes = pick_probes(5)
    print(f"Probing {len(probes)} booked leads via ?lead=<email> filter:\n")
    summary = []

    for p in probes:
        email = p["email"]
        print(f"=== {email}  ({p['client']}, csv: {p['ffups']} ffups + {p['eom']} eom) ===")
        # All emails (both directions)
        all_items = fetch_lead_emails(session, email)
        inbound = [it for it in all_items if it.get("ue_type") == 2]  # guess
        sent = [it for it in all_items if it.get("ue_type") in (1, 3)]
        print(f"  total items returned: {len(all_items)}")
        # Breakdown by ue_type
        by_ue = Counter(it.get("ue_type") for it in all_items)
        print(f"  ue_type counts: {dict(by_ue)}")
        # Breakdown by step (None = manual, "0_X_Y" = campaign auto)
        steps = Counter(it.get("step") for it in all_items)
        print(f"  step counts: {dict(steps)}")
        # Direction: who sent what?
        outbound = [it for it in all_items
                    if (it.get("eaccount") or "") and (it.get("eaccount") or "").lower() != email]
        inbound_real = [it for it in all_items
                        if (it.get("from_address_email") or "").lower() == email]
        print(f"  outbound (eaccount != lead): {len(outbound)}")
        print(f"  inbound  (from_address_email == lead): {len(inbound_real)}")
        manual_outbound = [it for it in outbound if it.get("step") is None or it.get("ue_type") == 3]
        auto_outbound = [it for it in outbound if it.get("step") is not None and it.get("ue_type") == 1]
        print(f"  outbound -> auto (has step): {len(auto_outbound)}, manual (no step): {len(manual_outbound)}")

        # Show one manual outbound if any
        if manual_outbound:
            m = manual_outbound[0]
            body = m.get("body") or {}
            bp = (body.get("text") or body.get("html") or "")[:200]
            print(f"  -- sample manual outbound --")
            print(f"     ts={m.get('timestamp_email')}, ue_type={m.get('ue_type')}, step={m.get('step')}")
            print(f"     subject={m.get('subject','')[:80]!r}")
            print(f"     body: {bp!r}")
            print(f"     -- CSV said ffup1 was: {p['ffup1']!r}")

        # Save raw for first lead
        if p == probes[0]:
            out = DEBUG_DIR / f"probe_v4_full_{email.replace('@','_at_')}.json"
            out.write_text(json.dumps(all_items, indent=2, default=str), encoding="utf-8")
            print(f"  raw → {out}")

        summary.append({
            "lead": email,
            "client": p["client"],
            "csv_ffups": p["ffups"],
            "csv_eom": p["eom"],
            "api_total": len(all_items),
            "api_outbound_auto": len(auto_outbound),
            "api_outbound_manual": len(manual_outbound),
            "api_inbound": len(inbound_real),
        })
        print()
        time.sleep(0.8)

    # Final summary
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"{'Lead':<40} {'CSV(f+e)':<10} {'Auto':<6} {'Manual':<8} {'Inbound':<8}")
    for r in summary:
        csv_total = r["csv_ffups"] + r["csv_eom"]
        print(f"{r['lead']:<40} {csv_total:<10} {r['api_outbound_auto']:<6} "
              f"{r['api_outbound_manual']:<8} {r['api_inbound']:<8}")

    total_csv = sum(r["csv_ffups"] + r["csv_eom"] for r in summary)
    total_manual = sum(r["api_outbound_manual"] for r in summary)
    total_auto = sum(r["api_outbound_auto"] for r in summary)
    print()
    print(f"Totals: CSV manual ffups = {total_csv}, API manual outbound = {total_manual}, "
          f"API auto outbound = {total_auto}")

    # Write summary markdown
    out = DEBUG_DIR / "probe_outbound_v4_summary.md"
    with open(out, "w", encoding="utf-8") as f:
        f.write(f"# Instantly outbound probe v4 — final\n\n")
        f.write(f"Run at: {datetime.now(timezone.utc).isoformat()}\n\n")
        f.write("## Method\n\n")
        f.write("- Use `GET /v2/emails?lead=<email>&limit=100` — confirmed working as a per-lead filter.\n")
        f.write("- Classify each returned item: outbound = `eaccount != lead`, manual = `step IS NULL` or `ue_type == 3`.\n\n")
        f.write("## Per-lead results\n\n")
        f.write("| Lead | Client | CSV ffups+eom | API auto out | API manual out | API inbound |\n")
        f.write("|---|---|---|---|---|---|\n")
        for r in summary:
            f.write(f"| {r['lead']} | {r['client']} | "
                    f"{r['csv_ffups'] + r['csv_eom']} | {r['api_outbound_auto']} | "
                    f"{r['api_outbound_manual']} | {r['api_inbound']} |\n")
        f.write("\n## Verdict\n\n")
        if total_manual >= total_csv * 0.7:
            f.write(f"**GREEN.** API exposes manual outbound sends with strong coverage "
                    f"({total_manual} API manuals vs. {total_csv} CSV manuals — "
                    f"{100*total_manual/max(total_csv,1):.0f}%).\n\n")
            f.write("Plan-as-written stands. Phase 2 = extend `instantly_sync.py` with "
                    "`email_type=sent` pass + per-lead pagination as needed.\n")
        elif total_manual > 0:
            f.write(f"**YELLOW.** Manual sends visible but partial: "
                    f"{total_manual}/{total_csv} = {100*total_manual/max(total_csv,1):.0f}% coverage.\n\n")
            f.write("Investigate gaps — likely a body-text-missing issue, NOT a missing-row issue.\n")
        else:
            f.write("**RED.** No manual outbound found via API. Need webhook capture.\n")
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
