"""Final round: discover which e-com industry strings are actually accepted.
Each rejection is free (400 with filter_error tells us what's invalid).
We only spend credits on calls that return 200."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests
from dotenv import load_dotenv

from prospeo_sync import PROSPEO_BASE


def call(filters: dict, api_key: str) -> tuple[int, dict]:
    url = f"{PROSPEO_BASE}/search-person"
    r = requests.post(url, json={"filters": filters, "page": 1},
                       headers={"X-KEY": api_key, "Content-Type": "application/json"},
                       timeout=20)
    try:
        body = r.json()
    except ValueError:
        body = {}
    return r.status_code, body


def probe_industry(value: str, api_key: str) -> str:
    status, body = call({"company_industry": {"include": [value]}}, api_key)
    if status == 200:
        tc = (body.get("pagination") or {}).get("total_count")
        return f"OK (HTTP 200, total_count={tc}) — credit spent"
    elif status == 400:
        fe = body.get("filter_error") or body.get("error_toast") or ""
        return f"REJECTED — {fe}"
    return f"HTTP {status}: {json.dumps(body)[:200]}"


def main() -> None:
    load_dotenv()
    api_key = os.environ.get("PROSPEO_API_KEY", "").strip()
    if not api_key:
        sys.exit("PROSPEO_API_KEY not set in .env")

    # Candidates spanning e-com brands: apparel, beauty, food, home, sports, etc.
    # Mix of LinkedIn's old (pre-2023) and new (post-2023) taxonomy names.
    candidates = [
        # Apparel-ish
        "Retail Apparel and Fashion",
        "Apparel Manufacturing",
        "Apparel & Fashion",
        "Fashion",
        "Apparel",
        "Retail",
        # Beauty / personal care
        "Cosmetics",
        "Personal Care Product Manufacturing",
        "Personal Care Products",
        "Health and Beauty",
        # Food / beverage
        "Food and Beverage Services",
        "Food and Beverage Manufacturing",
        "Food Production",
        "Beverage Manufacturing",
        # Home / furniture
        "Furniture and Home Furnishings Manufacturing",
        "Retail Home Furnishings",
        "Furniture",
        # Sports / outdoors
        "Sporting Goods Manufacturing",
        "Spectator Sports",
        # Consumer goods catchall
        "Consumer Goods",
        "Consumer Services",
        "Retail Consumer Goods",
        # Pet
        "Pet Services",
        # Health / wellness
        "Wellness and Fitness Services",
        "Health, Wellness & Fitness",
        # Generic manufacturing
        "Manufacturing",
        # Known-working sanity check
        "Software Development",
    ]

    print("Probing industry enum values (each rejection is FREE):")
    valid: list[str] = []
    invalid: list[str] = []
    for v in candidates:
        result = probe_industry(v, api_key)
        if result.startswith("OK"):
            valid.append(v)
        else:
            invalid.append(v)
        print(f"  {v!r:55s} {result}")

    print(f"\n=== Verified ACCEPTED ({len(valid)}): ===")
    for v in valid:
        print(f"  - {v}")
    print(f"\n=== Verified REJECTED ({len(invalid)}): ===")
    for v in invalid:
        print(f"  - {v}")


if __name__ == "__main__":
    main()
