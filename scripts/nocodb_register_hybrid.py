"""Re-register followup_tracker_mv (53 cols) and register followup_messages_mv (9 cols)
in NocoDB after the hybrid migration.

Sequence:
  1. Find existing followup_tracker_mv registration(s) on solar_grasshopper
     and on endless_egret (the duplicate from last session)
  2. Delete those registrations (the PG view stays intact — only the NocoDB
     metadata link gets removed)
  3. Toggle is_schema_readonly off on solar_grasshopper
  4. POST new registration for followup_tracker_mv (53 cols)
  5. POST new registration for followup_messages_mv (9 cols)
  6. Restore is_schema_readonly on

Credentials passed via env vars (NOCODB_URL, NOCODB_EMAIL, NOCODB_PASSWORD).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import connect


def get_columns_for_view(conn, view_name: str) -> list[dict]:
    """Read PG schema and convert to NocoDB column specs (preserving order)."""
    cur = conn.cursor()
    cur.execute("""
        select column_name, data_type
        from information_schema.columns
        where table_schema = 'public' and table_name = %s
        order by ordinal_position
    """, (view_name,))
    cols = cur.fetchall()
    out = []
    for name, pg_type in cols:
        if "time" in pg_type:
            uidt = "DateTime"
        elif pg_type == "integer" or pg_type == "bigint":
            uidt = "Number"
        elif pg_type == "text":
            uidt = "LongText"
        else:
            uidt = "SingleLineText"
        out.append({
            "column_name": name,
            "title": name,
            "uidt": uidt,
            "dt": pg_type,
        })
    return out


def main() -> None:
    base_url = os.environ.get("NOCODB_URL", "").rstrip("/")
    email = os.environ.get("NOCODB_EMAIL", "").strip()
    password = os.environ.get("NOCODB_PASSWORD", "").strip()
    if not (base_url and email and password):
        sys.exit("FATAL: set NOCODB_URL, NOCODB_EMAIL, NOCODB_PASSWORD env vars")

    r = requests.post(f"{base_url}/api/v1/auth/user/signin",
                      json={"email": email, "password": password}, timeout=30)
    r.raise_for_status()
    token = r.json()["token"]
    H = {"xc-auth": token, "Content-Type": "application/json"}

    BASE_ID = "py1poai31xooit5"
    PG_SOURCE = "bwzn08z3sbatror"  # solar_grasshopper

    # Step 1: find existing followup_tracker_mv registrations
    print("Step 1: finding existing followup_tracker_mv registrations...")
    r = requests.get(f"{base_url}/api/v2/meta/bases/{BASE_ID}/tables",
                     headers=H, timeout=30)
    matches = [t for t in r.json().get("list", [])
               if t.get("table_name") == "followup_tracker_mv"]
    for m in matches:
        print(f"  - id={m.get('id')} source={m.get('source_id')!r}")

    # Step 2: delete existing followup_tracker_mv registrations
    print("\nStep 2: deleting existing followup_tracker_mv registrations...")
    for m in matches:
        tid = m.get("id")
        r = requests.delete(f"{base_url}/api/v2/meta/tables/{tid}",
                            headers=H, timeout=30)
        print(f"  DELETE {tid} -> {r.status_code}  {r.text[:120]}")

    # Step 3: also delete any existing followup_messages_mv registrations (re-run safety)
    r = requests.get(f"{base_url}/api/v2/meta/bases/{BASE_ID}/tables",
                     headers=H, timeout=30)
    existing_msgs = [t for t in r.json().get("list", [])
                     if t.get("table_name") == "followup_messages_mv"]
    if existing_msgs:
        print("\nStep 3: deleting existing followup_messages_mv registrations...")
        for m in existing_msgs:
            tid = m.get("id")
            r = requests.delete(f"{base_url}/api/v2/meta/tables/{tid}",
                                headers=H, timeout=30)
            print(f"  DELETE {tid} -> {r.status_code}  {r.text[:120]}")

    # Step 4: toggle schema-readonly off so we can register
    print("\nStep 4: PATCH is_schema_readonly = 0 on solar_grasshopper...")
    r = requests.patch(f"{base_url}/api/v2/meta/bases/{BASE_ID}/sources/{PG_SOURCE}",
                       headers=H, json={"is_schema_readonly": 0}, timeout=30)
    print(f"  -> {r.status_code}")

    try:
        # Step 5: pull columns from PG and register followup_tracker_mv
        conn = connect()

        print("\nStep 5: register followup_tracker_mv (wide view)...")
        cols = get_columns_for_view(conn, "followup_tracker_mv")
        print(f"  PG view has {len(cols)} columns")
        body = {
            "table_name": "followup_tracker_mv",
            "title":      "followup_tracker_mv",
            "type":       "view",
            "columns":    cols,
        }
        r = requests.post(
            f"{base_url}/api/v1/db/meta/projects/{BASE_ID}/{PG_SOURCE}/tables",
            headers=H, json=body, timeout=60,
        )
        if r.ok:
            new_id = r.json().get("id")
            print(f"  [OK] registered as id={new_id}")
        else:
            print(f"  ! {r.status_code}  {r.text[:300]}")

        # Step 6: register followup_messages_mv
        print("\nStep 6: register followup_messages_mv (long-form view)...")
        cols = get_columns_for_view(conn, "followup_messages_mv")
        print(f"  PG view has {len(cols)} columns")
        body = {
            "table_name": "followup_messages_mv",
            "title":      "followup_messages_mv",
            "type":       "view",
            "columns":    cols,
        }
        r = requests.post(
            f"{base_url}/api/v1/db/meta/projects/{BASE_ID}/{PG_SOURCE}/tables",
            headers=H, json=body, timeout=60,
        )
        if r.ok:
            new_id = r.json().get("id")
            print(f"  [OK] registered as id={new_id}")
        else:
            print(f"  ! {r.status_code}  {r.text[:300]}")

        conn.close()

    finally:
        # Step 7: restore schema-readonly
        print("\nStep 7: PATCH is_schema_readonly = 1 (restore)...")
        r = requests.patch(f"{base_url}/api/v2/meta/bases/{BASE_ID}/sources/{PG_SOURCE}",
                           headers=H, json={"is_schema_readonly": 1}, timeout=30)
        print(f"  -> {r.status_code}")

    # Step 8: verify
    print("\nStep 8: verifying final state...")
    time.sleep(1)
    r = requests.get(f"{base_url}/api/v2/meta/bases/{BASE_ID}/tables",
                     headers=H, timeout=30)
    relevant = [t for t in r.json().get("list", [])
                if "followup" in (t.get("table_name") or "").lower()]
    for t in relevant:
        print(f"  - id={t.get('id')} table_name={t.get('table_name')!r}  "
              f"source={t.get('source_id')!r}")


if __name__ == "__main__":
    main()
