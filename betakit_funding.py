import argparse
import collections
import csv
import html
import json
import re
import sys
import time
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from common import CACHE, CITY_PROVINCE, SOURCES, UA, normalize

API = (
    "https://betakit.com/wp-json/wp/v2/posts?tags=2252&after={after}T00:00:00"
    "&per_page=100&page={page}&_fields=id,date,title,link,content"
)
VERB = re.compile(
    r"\s(?:raises|raised|secures|secured|closes|closed|lands|landed|nets|nabs|nab|bags|scores|snags|grabs"
    r"|picks up|completes|receives|gets|adds|announces|announced|eyes|exits|becomes)\s",
    re.I,
)
AMOUNT = re.compile(r"\$\s?([\d.,]+)(?:[-\s]?(million|billion|thousand|m|b|k)\b)?", re.I)
ROUND = re.compile(r"pre-seed|seed|series [a-h]\b|growth|bridge|venture debt|credit facility|debt|grant|extension", re.I)
NOT_A_STARTUP = re.compile(
    r"\bVC fund\b|\bventure fund\b|\bfund (?:I|II|III|IV|V)\b|(?:first|second|third|fourth|fifth|final|debut)[^$]*\bfund\b"
    r"|capital commitments|\bcommits\b|\bLPs?\b|government|\bfor \d+ (?:businesses|companies|startups)\b"
    r"|\bfor Canadian (?:manufacturers|companies|businesses|startups)\b|\bchallenge\b|\bprogram\b|\bstrategy\b",
    re.I,
)
INVESTOR_NAME = re.compile(r"\b(?:Ventures?|Capital|Partners|Fund|VC|Investments?|Asset Management)\b")
INVESTOR_DOMAIN = re.compile(r"\.vc$|ventures|capital|partners|invest|fund")
DESCRIPTOR = re.compile(
    r"^(?:company|startup|start-up|scaleup|scale-up|platform|firm|maker|developer|provider|fintech|healthtech"
    r"|insurtech|proptech|edtech|cleantech|agtech|biotech|medtech|legaltech|regtech|unicorn|spinout|spin-off"
    r"|grad|graduate|alum|alumnus)$|-(?:based|backed|founded|born)$",
    re.I,
)
TRAILING_PEOPLE = {"founders", "founder", "co-founders", "team"}
CANADIAN_ENTITY = re.compile(
    r"\bCanadian(?:-founded)? (?:startup|company|firm|scaleup|scale-up|fintech|healthtech|insurtech|proptech|edtech"
    r"|cleantech|agtech|biotech|medtech|AI|software|tech)\b|\bCanada-based\b",
    re.I,
)
IGNORED_DOMAINS = (
    "betakit.com", "twitter.com", "x.com", "linkedin.com", "facebook.com", "instagram.com", "youtube.com",
    "youtu.be", "tiktok.com", "threads.net", "bsky.app", "crunchbase.com", "google.com", "wikipedia.org",
    "newswire.ca", "globenewswire.com", "businesswire.com", "prnewswire.com", "cision.com", "cbc.ca",
    "theglobeandmail.com", "financialpost.com", "bnnbloomberg.ca", "bloomberg.com", "techcrunch.com",
    "reuters.com", "thelogic.co", "medium.com", "substack.com", "canada.ca", "gc.ca", "ontario.ca",
    "sedarplus.ca", "sec.gov", "apple.com", "eventbrite.com", "lu.ma", "wsj.com", "ft.com", "forbes.com",
    "cnbc.com", "nytimes.com", "thestar.com", "bdc.ca", "edc.ca", "citynews.ca", "unsplash.com",
)
CITY =r"([A-ZÀ-Ý][\w'’.é-]+(?:\s[A-Z][\w'’.é-]+)?)"
LOCATION_PATTERNS = [re.compile(CITY + r"-based"), re.compile(r"(?:based|headquartered) in " + CITY)]
FIELDS = [
    "Company", "Website", "Confiance site", "Company City", "Company State", "Company Country",
    "Last Raised At", "Montant", "Devise", "Tour", "Titre article", "Source",
    "Usage (CDI / Freelance / À décider)", "Keywords",
]


def norm(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def fetch_posts(after):
    posts, page, pages = [], 1, 1
    while page <= pages:
        req = urllib.request.Request(API.format(after=after, page=page), headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=60) as resp:
            pages = int(resp.headers.get("X-WP-TotalPages", 1))
            posts += json.loads(resp.read())
        print(f"  BetaKit page {page}/{pages}", flush=True)
        page += 1
        time.sleep(2)
    return posts


def company_from_title(title):
    match = VERB.search(title)
    if not match:
        return ""
    words = title[: match.start()].rsplit(":", 1)[-1].split()
    if words and words[-1].lower() in TRAILING_PEOPLE:
        words = words[:-1]
    if words and re.search(r"['’]s$", words[0]):
        words = words[1:]
    last_descriptor = max((i for i, w in enumerate(words) if DESCRIPTOR.search(w.strip(","))), default=-1)
    name = " ".join(words[last_descriptor + 1:]).strip(" ,'’\"")
    if not name or len(name) > 40 or not (name[0].isupper() or name[0].isdigit()):
        return ""
    return name


def locate(text):
    head = text[:1500]
    for pattern in LOCATION_PATTERNS:
        for match in pattern.finditer(head):
            city = match.group(1).replace("’", "'")
            province = CITY_PROVINCE.get(city.lower())
            if province:
                return city, province
    return "", ""


def website(content, company):
    target = norm(company)
    for href, label in re.findall(r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', content, re.S):
        domain = normalize(html.unescape(href))
        if not domain or domain.endswith(".gov") or any(domain == d or domain.endswith("." + d) for d in IGNORED_DOMAINS):
            continue
        label = norm(re.sub(r"<[^>]+>", "", html.unescape(label)))
        if target and (target in norm(domain) or label == target):
            return domain
    return ""


DEFAULT_OUT = SOURCES / "betakit.csv"


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Liste les startups canadiennes qui ont annoncé une levée de fonds sur BetaKit.")
    parser.add_argument("--months", type=int, default=12)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--refresh", action="store_true", help="re-télécharge les articles au lieu d'utiliser le cache du jour")
    args = parser.parse_args()
    run(args.months, Path(args.out), args.refresh)


def run(months=12, out=DEFAULT_OUT, refresh=False):
    """Télécharge (ou relit le cache du jour) et écrit la liste. Renvoie le chemin du CSV."""
    after = (date.today() - timedelta(days=30 * months)).isoformat()
    cache = CACHE / f"betakit-funding-{after}.json"
    if cache.exists() and not refresh:
        posts = json.loads(cache.read_text(encoding="utf-8"))
    else:
        posts = fetch_posts(after)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(posts, ensure_ascii=False), encoding="utf-8")
        for old in CACHE.glob("betakit-funding-*.json"):
            if old != cache:
                old.unlink()

    rows, skipped = {}, collections.Counter()
    for post in posts:
        title = html.unescape(post["title"]["rendered"])
        content = post["content"]["rendered"]
        text = html.unescape(re.sub(r"<[^>]+>", " ", content))
        company = company_from_title(title)
        amount = AMOUNT.search(title)
        if not company or not amount or NOT_A_STARTUP.search(title):
            skipped["pas une levée de startup"] += 1
            continue
        site = website(content, company)
        if INVESTOR_NAME.search(company) or (site and INVESTOR_DOMAIN.search(site)):
            skipped["fonds ou investisseur"] += 1
            continue
        city, province = locate(text)
        if not province and not CANADIAN_ENTITY.search(text[:1200]):
            skipped["hors Canada ou localisation introuvable"] += 1
            continue
        key = norm(company)
        if key in rows:
            skipped["doublon (levée plus ancienne)"] += 1
            continue
        round_match = ROUND.search(title) or ROUND.search(text[:600])
        unit = (amount.group(2) or "").lower()
        rows[key] = {
            "Company": company,
            "Website": site,
            "Confiance site": "lien de l'article" if site else "à trouver",
            "Company City": city,
            "Company State": province,
            "Company Country": "Canada",
            "Last Raised At": post["date"][:10],
            "Montant": f"{amount.group(1)} {unit}".strip(),
            "Devise": "USD" if "USD" in title else "CAD",
            "Tour": round_match.group(0).title() if round_match else "",
            "Titre article": title,
            "Source": post["link"],
            "Usage (CDI / Freelance / À décider)": "",
            "Keywords": title,
        }

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    user_fields = ["Usage (CDI / Freelance / À décider)"]
    if out.exists():
        from score_prospects import RANK_FIELDS

        with open(out, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            # Hand-added columns (Sélection, Notes…) survive a refresh; ranking columns are recomputed by score_prospects.py.
            user_fields += [c for c in reader.fieldnames or [] if c not in FIELDS and c not in RANK_FIELDS]
            previous = {norm(r.get("Company")): r for r in reader}
        for key, row in rows.items():
            old = previous.get(key, {})
            for field in user_fields:
                row[field] = old.get(field, "")
            if not row["Website"] and old.get("Website"):
                row["Website"], row["Confiance site"] = old["Website"], old.get("Confiance site", "")
        for key, old in previous.items():
            if key not in rows and any(old.get(field) for field in user_fields):
                rows[key] = {field: old.get(field, "") for field in FIELDS + user_fields}
    fieldnames = FIELDS + [field for field in user_fields if field not in FIELDS]
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows.values())

    kept = list(rows.values())
    print(f"\n{len(posts)} articles · {len(kept)} startups canadiennes retenues")
    for reason, count in skipped.most_common():
        print(f"  écartés — {reason} : {count}")
    print(f"  site trouvé : {sum(bool(r['Website']) for r in kept)} · à trouver : {sum(not r['Website'] for r in kept)}")
    print("  provinces :", collections.Counter(r["Company State"] or "non précisée" for r in kept).most_common())
    print(f"\n→ {out}")
    return out


if __name__ == "__main__":
    main()
