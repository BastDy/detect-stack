"""Détecte HubSpot, les outils GTM concurrents, les indices SaaS et la page carrières sur le site d'une entreprise."""
import argparse
import csv
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin

from common import OUTPUT, UA, fetch, fetch_home, normalize  # noqa: F401  (UA/normalize réexportés)

SUBPAGES = ["/contact", "/contact-us", "/demo", "/pricing"]
CAREER_PAGES = ["/careers", "/jobs"]
SUFFIX = r"(?:-[a-z0-9]+)?"

HUBSPOT_STRONG = {
    "tracking": rf"js{SUFFIX}\.hs-scripts\.com",
    "formulaires": rf"js{SUFFIX}\.hsforms\.net|hbspt\.forms\.create|forms{SUFFIX}\.hsforms\.com",
    "cms": rf'hubspotusercontent|hs-sites{SUFFIX}\.com|hscoscdn|name="generator"\s+content="hubspot',
    "chat": rf"js{SUFFIX}\.usemessages\.com",
    "analytics": rf"js{SUFFIX}\.hs-analytics\.net",
    "banniere-cookies": rf"js{SUFFIX}\.hs-banner\.com",
    "formulaires-collectes": rf"js{SUFFIX}\.hscollectedforms\.net",
}
HUBSPOT_WEAK = {
    "lien-meetings": rf"meetings{SUFFIX}\.hubspot\.com",
    "_hsq": r"\b_hsq\b",
}
OTHER_TOOLS = {
    "Salesforce Pardot": r"pi\.pardot\.com|go\.pardot\.com|pardot\.com/pd\.js",
    "Salesforce Web-to-Lead": r"webto\.salesforce\.com|\.my\.salesforce\.com",
    "Marketo": r"munchkin\.marketo\.net|mktoforms",
    "Pipedrive": r"leadbooster-chat\.pipedrive\.com|webforms\.pipedrive\.com|pipedriveassets\.com",
    "Zoho": r"salesiq\.zoho|zohopublic|zohocdn",
    "ActiveCampaign": r"trackcmp\.net|activehosted\.com",
    "Mailchimp": r"chimpstatic\.com|list-manage\.com",
    "Intercom": r"widget\.intercom\.io|js\.intercomcdn\.com",
    "Drift": r"js\.driftt\.com",
    "Calendly": r"assets\.calendly\.com|calendly\.com/",
    "Typeform": r"embed\.typeform\.com|form\.typeform\.com",
    "Google Tag Manager": r"googletagmanager\.com/gtm\.js",
}
SAAS_HINTS = {
    "pricing": r'href="[^"]*/pricing',
    "demo": r"(?:book|request|get|schedule) (?:a )?demo",
    "essai-gratuit": r"free trial|start for free|try (?:it )?(?:for )?free",
    "app-login": r'href="https?://(?:app|login|dashboard|portal)\.',
}
ATS_LINKS = {
    "greenhouse": r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)?([a-z0-9_-]{2,})",
    "lever": r"jobs\.lever\.co/([a-z0-9_.-]{2,})",
    "ashby": r"jobs\.ashbyhq\.com/([a-z0-9_.-]{2,})",
    "workable": r"apply\.workable\.com/([a-z0-9_-]{2,})",
}
ATS_IGNORED_SLUGS = {"embed", "api", "v1", "js"}
PORTAL_ID = [r"hs-scripts\.com/(\d{4,10})\.js", r"portalid[\"']?\s*:\s*[\"']?(\d{4,10})"]
REGION = r"(?:js|hubspotusercontent|hs-sites)-(na2|na3|eu1|ap1)\b"
FIELDS = [
    "domaine", "hubspot", "portal_id", "region", "signaux_hubspot",
    "autres_outils", "indices_saas", "ats", "pages_analysees", "erreur",
]
VERDICT_ORDER = {"Oui": 0, "Probable": 1, "Non détecté": 2, "Erreur": 3}


def scan(text, found):
    low = text.lower()
    buckets = (
        ("strong", HUBSPOT_STRONG), ("weak", HUBSPOT_WEAK),
        ("other", OTHER_TOOLS), ("saas", SAAS_HINTS),
    )
    for bucket, patterns in buckets:
        for name, pattern in patterns.items():
            if re.search(pattern, low):
                found[bucket].add(name)
    for provider, pattern in ATS_LINKS.items():
        for slug in re.findall(pattern, low):
            if slug not in ATS_IGNORED_SLUGS:
                found["ats"].add(f"{provider}:{slug}")
    for pattern in PORTAL_ID:
        found["portal"].update(re.findall(pattern, low))
    found["region"].update(re.findall(REGION, low))


def analyze(domain, timeout):
    found = {key: set() for key in ("strong", "weak", "other", "saas", "ats", "portal", "region")}
    try:
        base, html = fetch_home(domain, timeout)
    except Exception as exc:
        return {"domaine": domain, "hubspot": "Erreur", "erreur": f"{type(exc).__name__}: {exc}"[:150]}

    pages = 1
    scan(html, found)
    for path in SUBPAGES:
        if found["strong"]:
            break
        try:
            _, page = fetch(urljoin(base, path), timeout)
        except Exception:
            continue
        pages += 1
        scan(page, found)

    # Many sites load HubSpot through Google Tag Manager, so the script never appears in the HTML.
    if not found["strong"]:
        for gtm_id in sorted(set(re.findall(r"GTM-[A-Z0-9]{4,10}", html)))[:2]:
            try:
                _, container = fetch(f"https://www.googletagmanager.com/gtm.js?id={gtm_id}", timeout)
            except Exception:
                continue
            before = set(found["strong"])
            scan(container, found)
            if found["strong"] - before:
                found["strong"].add("via-gtm")

    if not found["ats"]:
        for path in CAREER_PAGES:
            try:
                _, page = fetch(urljoin(base, path), timeout)
            except Exception:
                continue
            pages += 1
            scan(page, found)
            if found["ats"]:
                break

    if found["strong"]:
        verdict = "Oui"
    elif found["weak"]:
        verdict = "Probable"
    else:
        verdict = "Non détecté"
    hubspot_seen = verdict != "Non détecté"
    return {
        "domaine": domain,
        "hubspot": verdict,
        "portal_id": ";".join(sorted(found["portal"])) if hubspot_seen else "",
        "region": ";".join(sorted(found["region"])) if hubspot_seen else "",
        "signaux_hubspot": ", ".join(sorted(found["strong"] | found["weak"])),
        "autres_outils": ", ".join(sorted(found["other"])),
        "indices_saas": ", ".join(sorted(found["saas"])),
        "ats": ";".join(sorted(found["ats"])),
        "pages_analysees": pages,
        "erreur": "",
    }


def domains_from_file(path):
    path = Path(path)
    if path.suffix.lower() != ".csv":
        return path.read_text(encoding="utf-8").splitlines()
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        column = next(
            (c for c in reader.fieldnames or [] if c.strip().lower() in {"domain", "domaine", "website", "site", "url"}),
            None,
        )
        if column is None:
            sys.exit(f"{path} : aucune colonne domain / domaine / website / site / url")
        return [row[column] for row in reader]


def domains_from_tracker(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    domains = []
    for row in rows:
        if row.get("Statut", "").startswith("Écarté"):
            continue
        match = re.search(r"\(([^()\s]+\.[a-z]{2,})\)", row.get("Entreprise", ""), re.I)
        if match:
            domains.append(match.group(1))
    return domains


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Détecte HubSpot et les outils concurrents sur les sites d'une liste d'entreprises.")
    parser.add_argument("domains", nargs="*", help="domaines à analyser (ex. acme.com)")
    parser.add_argument("--file", help="fichier .txt (un domaine par ligne) ou .csv (colonne domain/domaine/website/site/url)")
    parser.add_argument("--tracker", help="suivi-prospects.csv : lit le domaine entre parenthèses dans la colonne Entreprise")
    parser.add_argument("--out", default=str(OUTPUT / "detection-hubspot.csv"))
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args()

    raw = list(args.domains)
    if args.file:
        raw += domains_from_file(args.file)
    if args.tracker:
        raw += domains_from_tracker(args.tracker)
    domains = list(dict.fromkeys(d for d in map(normalize, raw) if d))
    if not domains:
        parser.error("aucun domaine fourni")

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(lambda d: analyze(d, args.timeout), domains))
    rows.sort(key=lambda r: (VERDICT_ORDER[r["hubspot"]], r["domaine"]))

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, restval="")
        writer.writeheader()
        writer.writerows(rows)

    for r in rows:
        details = r.get("signaux_hubspot") or r.get("erreur") or ""
        others = r.get("autres_outils", "")
        print(f"{r['hubspot']:<12} {r['domaine']:<30} {details}" + (f"  | autres : {others}" if others else ""))
    counts = {v: sum(r["hubspot"] == v for r in rows) for v in VERDICT_ORDER}
    print(f"\n{counts['Oui']} Oui · {counts['Probable']} Probable · {counts['Non détecté']} Non détecté · {counts['Erreur']} Erreur → {args.out}")


if __name__ == "__main__":
    main()
