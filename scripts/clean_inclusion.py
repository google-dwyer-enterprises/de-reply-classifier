"""One-shot: filter inclusion list, drop rows whose Website or Emails column
contains a blocked TLD / freemail domain / mega-brand.

Usage: python scripts/clean_inclusion.py <in.csv> <out.csv>
"""
from __future__ import annotations

import csv
import sys

BLOCKED_SUFFIXES = (".org", ".edu", ".io")
BLOCKED_DOMAINS = ("gmail.com", "yahoo.com", "outlook.com")

# Mega-brands / Fortune-500 / global enterprises that don't buy from
# Amazon-agency outreach. They have in-house teams and swamp Prospeo's API
# (100+ exec-titled people each), so they also break batched searches.
# Extend as we find more.
MEGA_BRANDS = {
    # Tech / SaaS giants
    "google.com", "alphabet.com", "microsoft.com", "apple.com", "meta.com",
    "facebook.com", "amazon.com", "oracle.com", "salesforce.com", "ibm.com",
    "intel.com", "adobe.com", "sap.com", "cisco.com", "hp.com", "dell.com",
    "nvidia.com", "samsung.com", "sony.com", "lg.com",
    # Big retail / marketplaces (already blocked elsewhere but include here)
    "walmart.com", "target.com", "costco.com", "kroger.com", "homedepot.com",
    "lowes.com", "bestbuy.com", "macys.com", "nordstrom.com", "tjmaxx.com",
    "ebay.com", "etsy.com", "shopify.com", "faire.com", "alibaba.com",
    "aliexpress.com", "wayfair.com", "wish.com", "temu.com", "shein.com",
    # Apparel mega (in-house teams)
    "adidas.com", "nike.com", "underarmour.com", "puma.com", "reebok.com",
    "levi.com", "levis.com", "gap.com", "oldnavy.com", "hm.com", "zara.com",
    "uniqlo.com", "burberry.com", "ralphlauren.com", "tommy.com", "guess.com",
    "lulus.com", "lululemon.com",
    # CPG / food giants
    "pepsi.com", "pepsico.com", "coca-cola.com", "cocacola.com", "pg.com",
    "unilever.com", "kraftheinzcompany.com", "kelloggs.com", "kelloggcompany.com",
    "generalmills.com", "nestle.com", "danone.com", "mondelezinternational.com",
    "tysonfoods.com", "smithfield.com", "hersheys.com", "mars.com",
    # Ag / industrial / chemicals
    "adm.com", "cargill.com", "agcocorp.com", "deere.com", "3m.com", "dow.com",
    "ge.com", "honeywell.com", "siemens.com", "boeing.com", "lockheedmartin.com",
    # Auto
    "ford.com", "gm.com", "tesla.com", "toyota.com", "honda.com", "bmw.com",
    "mercedes-benz.com", "vw.com", "volkswagen.com", "nissan.com", "audi.com",
    # Pharma / health
    "pfizer.com", "jnj.com", "merck.com", "novartis.com", "roche.com",
    "abbvie.com", "bms.com", "lilly.com", "gilead.com", "gsk.com",
    # Financial / payments
    "chase.com", "bankofamerica.com", "wellsfargo.com", "citi.com", "ml.com",
    "goldmansachs.com", "morganstanley.com", "amex.com", "visa.com",
    "mastercard.com", "paypal.com", "stripe.com", "square.com", "affirm.com",
    # Telecom / media
    "verizon.com", "att.com", "tmobile.com", "sprint.com", "comcast.com",
    "spectrum.com", "disney.com", "netflix.com", "warnerbros.com",
    "paramount.com", "nbcuniversal.com",
    # Travel / hospitality
    "marriott.com", "hilton.com", "hyatt.com", "airbnb.com", "expedia.com",
    "booking.com", "delta.com", "united.com", "americanairlines.com",
}


def _host(value: str) -> str:
    """Strip scheme/path/www to get just the host."""
    v = (value or "").strip().lower()
    if not v:
        return ""
    if "://" in v:
        v = v.split("://", 1)[1]
    for sep in ("/", "?", "#"):
        if sep in v:
            v = v.split(sep, 1)[0]
    if v.startswith("www."):
        v = v[4:]
    if "@" in v:
        v = v.rsplit("@", 1)[1]
    return v


def _is_mega_brand(host: str) -> bool:
    """Match the registrable domain against MEGA_BRANDS (also catches subdomains
    of mega-brands like store.adidas.com)."""
    if not host:
        return False
    if host in MEGA_BRANDS:
        return True
    # Also catch subdomains: parts.agcocorp.com → agcocorp.com
    parts = host.split(".")
    for i in range(len(parts) - 1):
        candidate = ".".join(parts[i:])
        if candidate in MEGA_BRANDS:
            return True
    return False


def is_blocked(value: str) -> bool:
    v = _host(value)
    if not v:
        return False
    if v in BLOCKED_DOMAINS:
        return True
    if any(v.endswith(suf) for suf in BLOCKED_SUFFIXES):
        return True
    if _is_mega_brand(v):
        return True
    return False


def main(src: str, dst: str) -> None:
    kept = 0
    dropped_tld = 0
    dropped_mega = 0
    mega_examples: list[str] = []
    with open(src, newline="", encoding="utf-8", errors="replace") as fin, \
         open(dst, "w", newline="", encoding="utf-8") as fout:
        rdr = csv.DictReader(fin)
        if not rdr.fieldnames:
            sys.exit(f"{src}: no header row found")
        wtr = csv.DictWriter(fout, fieldnames=rdr.fieldnames)
        wtr.writeheader()

        site_col = next((h for h in rdr.fieldnames if h.lower() in ("website", "url", "site")), None)
        email_col = next((h for h in rdr.fieldnames if h.lower() in ("emails", "email")), None)
        if not site_col and not email_col:
            sys.exit(f"{src}: no Website or Emails column found in {rdr.fieldnames}")

        for row in rdr:
            site = row.get(site_col, "") if site_col else ""
            email = row.get(email_col, "") if email_col else ""
            site_host = _host(site)
            email_host = _host(email)
            if _is_mega_brand(site_host) or _is_mega_brand(email_host):
                dropped_mega += 1
                if len(mega_examples) < 10:
                    mega_examples.append(site_host or email_host)
                continue
            if is_blocked(site) or is_blocked(email):
                dropped_tld += 1
                continue
            wtr.writerow(row)
            kept += 1

    print(f"kept           {kept:>8}")
    print(f"dropped (TLD)  {dropped_tld:>8}")
    print(f"dropped (mega) {dropped_mega:>8}")
    if mega_examples:
        print("  mega-brand examples:")
        for ex in mega_examples:
            print(f"    - {ex}")
    print(f"output  {dst}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: python scripts/clean_inclusion.py <in.csv> <out.csv>")
    main(sys.argv[1], sys.argv[2])
