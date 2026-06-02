"""Inspect NocoDB source detail + try alternative endpoints for MV registration."""

import json
import os
import sys

import requests


def main() -> None:
    base_url = os.environ.get("NOCODB_URL", "").rstrip("/")
    email = os.environ.get("NOCODB_EMAIL", "").strip()
    password = os.environ.get("NOCODB_PASSWORD", "").strip()

    r = requests.post(f"{base_url}/api/v1/auth/user/signin",
                      json={"email": email, "password": password}, timeout=30)
    r.raise_for_status()
    token = r.json()["token"]
    headers = {"xc-auth": token, "Content-Type": "application/json"}

    base_id = "py1poai31xooit5"
    source_id = "bwzn08z3sbatror"  # solar_grasshopper

    # 1. Source detail
    print("=== Source detail ===")
    r = requests.get(
        f"{base_url}/api/v2/meta/bases/{base_id}/sources/{source_id}",
        headers=headers, timeout=30,
    )
    if r.ok:
        print(json.dumps(r.json(), indent=2)[:2000])
    else:
        print(f"  ! {r.status_code}: {r.text[:300]}")

    # 2. Try meta-diff-sync endpoints
    print("\n=== Trying meta-diff-sync variants ===")
    endpoints = [
        ("GET",  f"/api/v2/meta/bases/{base_id}/sources/{source_id}/meta-diff/sync"),
        ("POST", f"/api/v2/meta/bases/{base_id}/sources/{source_id}/meta-diff/sync"),
        ("GET",  f"/api/v1/db/meta/projects/{base_id}/meta-diff/{source_id}/sync"),
        ("POST", f"/api/v1/db/meta/projects/{base_id}/meta-diff/{source_id}/sync"),
    ]
    for method, path in endpoints:
        r = requests.request(method, f"{base_url}{path}", headers=headers, timeout=30)
        print(f"  {method:4s} {path}  -> {r.status_code}  {r.text[:150]}")

    # 3. List tables (REST) — see if followup_tracker_mv is there but hidden
    print("\n=== Tables on source (REST GET) ===")
    r = requests.get(
        f"{base_url}/api/v2/meta/bases/{base_id}/sources/{source_id}/tables",
        headers=headers, timeout=30,
    )
    if r.ok:
        tables = r.json().get("list") or r.json().get("data") or []
        for t in tables:
            print(f"  - {t.get('table_name')!r}  title={t.get('title')!r}  enabled={t.get('enabled')}")
    else:
        print(f"  ! {r.status_code}: {r.text[:200]}")

    # 4. Try v2 with explicit "tables" path that triggers a refresh
    print("\n=== Listing all tables (without source filter) ===")
    r = requests.get(
        f"{base_url}/api/v2/meta/bases/{base_id}/tables?source_id={source_id}",
        headers=headers, timeout=30,
    )
    if r.ok:
        tables = r.json().get("list") or r.json().get("data") or []
        for t in tables:
            print(f"  - {t.get('table_name')!r}  title={t.get('title')!r}")
    else:
        print(f"  ! {r.status_code}: {r.text[:200]}")


if __name__ == "__main__":
    main()
