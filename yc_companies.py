"""Liste les entreprises Y Combinator actives basées au Canada.

Source : API publique yc-oss (miroir de l'annuaire YC), pas de clé nécessaire.
Usage : python yc_companies.py [--out data/sources/yc.csv] [--refresh]
"""
import argparse
import csv
import json
import sys
from datetime import date
from pathlib import Path

from common import CACHE, PROVINCE_CODES, SOURCES, fetch

API = "https://yc-oss.github.io/api/companies/all.json"
DEFAULT_OUT = SOURCES / "yc.csv"
FIELDS = [
    "Company", "Website", "# Employees", "Industry", "Company City", "Company State", "Company Country",
    "Keywords", "Company Linkedin Url", "YC batch", "YC hiring",
]


def canadian_location(locations):
    """'San Francisco, CA, USA; Toronto, ON, Canada' -> ('Toronto', 'Ontario')."""
    for loc in (locations or "").split(";"):
        parts = [p.strip() for p in loc.split(",")]
        if parts and parts[-1].lower() == "canada":
            city = parts[0] if len(parts) >= 2 else ""
            code = parts[1].upper() if len(parts) >= 3 else ""
            return city, PROVINCE_CODES.get(code, code)
    return None


def to_row(company):
    location = canadian_location(company.get("all_locations"))
    if not location or company.get("status") not in {"Active", "Public"}:
        return None
    city, province = location
    industry = company.get("industry") or ""
    sub = company.get("subindustry") or ""
    keywords = company.get("one_liner") or ""
    if company.get("tags"):
        keywords += " | " + ", ".join(company["tags"])
    return {
        "Company": company.get("name", ""),
        "Website": company.get("website", ""),
        "# Employees": company.get("team_size") or "",
        "Industry": f"{industry} / {sub}" if sub else industry,
        "Company City": city,
        "Company State": province,
        "Company Country": "Canada",
        "Keywords": keywords,
        "Company Linkedin Url": "",
        "YC batch": company.get("batch", ""),
        "YC hiring": company.get("isHiring", ""),
    }


def run(out=DEFAULT_OUT, refresh=False, timeout=60):
    cache = CACHE / f"yc-all-{date.today().isoformat()}.json"
    if cache.exists() and not refresh:
        data = json.loads(cache.read_text(encoding="utf-8"))
    else:
        _, text = fetch(API, timeout, accept="application/json", max_bytes=200_000_000)
        data = json.loads(text)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(text, encoding="utf-8")
        for old in CACHE.glob("yc-all-*.json"):
            if old != cache:
                old.unlink()

    rows = [r for r in map(to_row, data) if r and r["Website"]]
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"YC : {len(data)} entreprises lues · {len(rows)} actives au Canada → {out}")
    return out


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--refresh", action="store_true", help="re-télécharge l'annuaire même s'il est en cache aujourd'hui")
    args = parser.parse_args()
    run(Path(args.out), args.refresh)


if __name__ == "__main__":
    main()
