"""Récupère la page LinkedIn entreprise (souvent dans le footer) d'une liste de sites.

Même esprit que detect_stack.py : stdlib uniquement, entrée = domaines / .txt / .csv.
Sortie : find-linkedin.csv (domaine, linkedin, autres_liens, pages_analysees, erreur).
"""
import argparse
import csv
import html as htmllib
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote, urljoin

from common import OUTPUT, fetch, fetch_home, normalize

SUBPAGES = ["/contact", "/contact-us", "/about", "/about-us", "/company"]
FIELDS = ["domaine", "linkedin", "autres_liens", "pages_analysees", "erreur"]

LINKEDIN_RE = re.compile(
    r"https?:(?:\\?/){2}(?:[a-z]{2,3}\.|www\.)?linkedin\.com(?:\\?/)(company|school|showcase|in)(?:\\?/)([A-Za-z0-9%_.-]+)",
    re.I,
)
# Liens de partage / widgets, sans rapport avec la page de l'entreprise.
IGNORED_SLUGS = {"share", "sharearticle", "shareoffsite", "login", "signup", "feed", "jobs"}


def extract(text):
    """Renvoie la liste ordonnée des URLs LinkedIn trouvées (company d'abord, puis school/showcase/in)."""
    text = htmllib.unescape(text)
    # Le footer est en bas de page : on garde l'ordre inverse d'apparition comme tie-break.
    hits = []
    for pos, match in enumerate(LINKEDIN_RE.finditer(text)):
        kind, slug = match.group(1).lower(), unquote(match.group(2)).rstrip("/\\").strip()
        slug = slug.replace("\\", "")
        if not slug or slug.lower() in IGNORED_SLUGS:
            continue
        hits.append((kind, slug, pos))
    seen, result = set(), []
    for kind, slug, pos in sorted(hits, key=lambda h: (h[0] != "company", -h[2])):
        url = f"https://www.linkedin.com/{kind}/{slug}"
        if url.lower() not in seen:
            seen.add(url.lower())
            result.append(url)
    return result


def analyze(domain, timeout):
    try:
        base, page = fetch_home(domain, timeout)
    except Exception as exc:
        return {"domaine": domain, "erreur": f"{type(exc).__name__}: {exc}"[:150]}

    pages = 1
    links = extract(page)
    for path in SUBPAGES:
        if any("/company/" in u for u in links):
            break
        try:
            _, sub = fetch(urljoin(base, path), timeout)
        except Exception:
            continue
        pages += 1
        links += [u for u in extract(sub) if u not in links]

    companies = [u for u in links if "/company/" in u or "/showcase/" in u or "/school/" in u]
    main = companies[0] if companies else ""
    others = [u for u in links if u != main]
    return {
        "domaine": domain,
        "linkedin": main,
        "autres_liens": " ; ".join(others),
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


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Trouve la page LinkedIn entreprise sur les sites d'une liste de domaines.")
    parser.add_argument("domains", nargs="*", help="domaines à analyser (ex. acme.com)")
    parser.add_argument("--file", help="fichier .txt (un domaine par ligne) ou .csv (colonne domain/domaine/website/site/url)")
    parser.add_argument("--out", default=str(OUTPUT / "find-linkedin.csv"))
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args()

    raw = list(args.domains)
    if args.file:
        raw += domains_from_file(args.file)
    domains = list(dict.fromkeys(d for d in map(normalize, raw) if d))
    if not domains:
        parser.error("aucun domaine fourni")

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(lambda d: analyze(d, args.timeout), domains))
    rows.sort(key=lambda r: (bool(r.get("erreur")), not r.get("linkedin"), r["domaine"]))

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, restval="")
        writer.writeheader()
        writer.writerows(rows)

    for r in rows:
        print(f"{r['domaine']:<32} {r.get('linkedin') or r.get('erreur') or '(non trouvé)'}")
    found = sum(bool(r.get("linkedin")) for r in rows)
    errors = sum(bool(r.get("erreur")) for r in rows)
    print(f"\n{found} trouvés · {len(rows) - found - errors} non trouvés · {errors} erreurs → {args.out}")


if __name__ == "__main__":
    main()
