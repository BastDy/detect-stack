"""Score un export d'entreprises (Apollo, BetaKit, YC…) selon la grille ICP et propose un angle d'approche."""
import argparse
import csv
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path

from common import CACHE, CITY_PROVINCE, HERE, OUTPUT, Cache, fetch, load_config, normalize
from detect_stack import analyze

# TTL des caches : les offres d'emploi bougent vite, la stack technique beaucoup moins.
DETECTION_MAX_AGE_DAYS = 30
ATS_MAX_AGE_DAYS = 3

COLUMNS = {
    "company": ["company", "company name", "organization name", "account name", "entreprise"],
    "website": ["website", "company website", "domain", "company domain", "domaine", "site", "url"],
    "employees": ["# employees", "employees", "number of employees", "estimated num employees", "headcount"],
    "industry": ["industry", "industries", "secteur"],
    "keywords": ["keywords", "short description", "description", "seo description"],
    "country": ["company country", "country", "pays"],
    "state": ["company state", "state", "province", "region"],
    "city": ["company city", "city", "ville", "location (canada)", "location"],
    "technologies": ["technologies", "technology"],
    "last_raised": ["last raised at", "latest funding date", "last funding date"],
    "linkedin": ["company linkedin url", "linkedin url", "linkedin"],
}
SAAS_INDUSTRY = re.compile(r"software|internet|saas", re.I)
REVOPS_TITLE = re.compile(
    r"revenue operations|\brevops\b|sales operations|\bsales ops\b|marketing operations|\bmarketing ops\b"
    r"|gtm operations|go-to-market operations|customer success operations|crm (?:admin|administrator|manager|specialist)|hubspot|salesforce admin",
    re.I,
)
SALES_TITLE = re.compile(
    r"account executive|\bsales\b|business development|\b[bs]dr\b|account manager|marketing|growth|demand gen|customer success",
    re.I,
)
COMPETITOR_CRM = {"Salesforce Pardot", "Salesforce Web-to-Lead", "Marketo", "Pipedrive", "Zoho"}
ATS_API = {
    "greenhouse": ("https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", lambda d: [j["title"] for j in d.get("jobs", [])]),
    "lever": ("https://api.lever.co/v0/postings/{slug}?mode=json&limit=100", lambda d: [j["text"] for j in d] if isinstance(d, list) else []),
    "ashby": ("https://api.ashbyhq.com/posting-api/job-board/{slug}", lambda d: [j["title"] for j in d.get("jobs", [])]),
    "workable": ("https://apply.workable.com/api/v1/widget/accounts/{slug}", lambda d: [j["title"] for j in d.get("jobs", [])]),
}
TIER_ORDER = {"A": 0, "B": 1, "C": 2}
OUTPUT_FIELDS = [
    "tier", "score", "entreprise", "domaine", "detail_score", "angle_suggere",
    "offres_revops", "nb_offres_sales_marketing", "ats", "employes", "secteur",
    "ville", "province", "pays", "hubspot_site", "hubspot_apollo", "autres_outils",
    "indices_saas", "derniere_levee", "linkedin", "erreur_site",
]


def column_map(fieldnames):
    lookup = {name.strip().lower(): name for name in fieldnames or []}
    return {key: next((lookup[a] for a in aliases if a in lookup), None) for key, aliases in COLUMNS.items()}


def read_export(path):
    """Lit un CSV d'entreprises -> (records uniques par domaine, nb sans site, colonnes absentes).

    Lève ValueError si aucune colonne ne ressemble à un site web.
    """
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        cols = column_map(reader.fieldnames)
        if not cols["website"]:
            raise ValueError(f"{path} : colonne du site web introuvable (colonnes lues : {', '.join(reader.fieldnames or [])})")
        records, without_site = {}, 0
        for row in reader:
            rec = {key: (row.get(col) or "").strip() if col else "" for key, col in cols.items()}
            rec["domain"] = normalize(rec["website"])
            if not rec["domain"]:
                without_site += 1
                continue
            fill_location(rec)
            records.setdefault(rec["domain"], rec)
    missing = [key for key, col in cols.items() if col is None]
    return list(records.values()), without_site, missing


def fill_location(rec):
    """Déduit province et pays de la ville quand l'export ne les donne pas (ex. Apollo 'Location (Canada)')."""
    city = rec["city"].split(",")[0].strip().lower()
    province = CITY_PROVINCE.get(city)
    if province and not rec["state"]:
        rec["state"] = province
    if province and not rec["country"]:
        rec["country"] = "Canada"


def norm_company(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def default_tracker():
    """Chemin du tracker de candidatures : config.json > dossier voisin ai-job-search."""
    configured = load_config().get("job_tracker")
    if configured:
        return Path(configured)
    return HERE.parent / "ai-job-search" / "job_search_tracker.csv"


def applied_companies(path):
    """Entreprises où une candidature CDI existe déjà. Lève FileNotFoundError si le fichier manque."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Tracker de candidatures introuvable : {path}")
    with open(path, encoding="utf-8-sig", newline="") as f:
        return {norm_company(row.get("company")) for row in csv.DictReader(f)} - {""}


def run_stage(label, domains, worker, cache, workers):
    """Applique worker aux domaines absents ou expirés du cache, en parallèle, en sauvegardant au fil de l'eau."""
    todo = cache.stale(domains)
    print(f"{label} : {len(domains) - len(todo)} déjà en cache, {len(todo)} à traiter", flush=True)
    if todo:
        cache.path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache.path, "a", encoding="utf-8") as out, ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(worker, d): d for d in todo}
            for done, future in enumerate(as_completed(futures), 1):
                domain = futures[future]
                try:
                    result = future.result()
                except Exception as exc:  # un domaine qui plante ne doit pas arrêter le run
                    result = {"erreur": f"{type(exc).__name__}: {exc}"[:150]}
                cache.put(domain, result, out)
                if done % 100 == 0 or done == len(todo):
                    print(f"  {label} {done}/{len(todo)}", flush=True)
    cache.compact()


def fetch_text(url, timeout):
    return fetch(url, timeout, accept="application/json", max_bytes=5_000_000)[1]


def fetch_json(url, timeout):
    return json.loads(fetch_text(url, timeout))


def ats_candidates(rec, detection):
    linked = [item.split(":", 1) for item in (detection.get("ats") or "").split(";") if ":" in item]
    if linked:
        return linked, "lien du site"
    label = rec["domain"].split(".")[0]
    guesses = {label, label.replace("-", ""), norm_company(rec["company"])}
    return [(provider, g) for g in sorted(guesses) if len(g) >= 5 for provider in ATS_API], "slug deviné"


def board_belongs_to(rec, provider, slug, raw, payload, timeout):
    # A guessed slug like "bolster" can be another company's board: require the domain in the postings or a matching board name.
    if rec["domain"] in raw.lower():
        return True
    name = ""
    if provider == "greenhouse":
        try:
            name = fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}", timeout).get("name", "")
        except Exception:
            return False
    elif provider == "workable" and isinstance(payload, dict):
        name = payload.get("name", "")
    company, board = norm_company(rec["company"]), norm_company(name)
    return len(company) >= 5 and bool(board) and (board.startswith(company) or company.startswith(board))


def fetch_jobs(rec, detection, timeout):
    candidates, source = ats_candidates(rec, detection)
    for provider, slug in candidates:
        url, extract = ATS_API[provider]
        try:
            raw = fetch_text(url.format(slug=slug), timeout)
            payload = json.loads(raw)
            titles = extract(payload)
        except Exception:
            continue
        if source == "lien du site":
            return {"ats": f"{provider}:{slug} ({source})", "titles": titles[:300]}
        if titles and board_belongs_to(rec, provider, slug, raw, payload, timeout):
            return {"ats": f"{provider}:{slug} (slug deviné, vérifié)", "titles": titles[:300]}
    return {"ats": "", "titles": []}


def parse_int(value):
    match = re.search(r"\d[\d,]*", value or "")
    return int(match.group().replace(",", "")) if match else None


def parse_date(value):
    value = (value or "").strip()
    attempts = [(value[:10], "%Y-%m-%d")] + [(value, fmt) for fmt in ("%m/%d/%Y", "%d/%m/%Y", "%b %Y", "%B %Y")]
    for text, fmt in attempts:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def score(rec, det, jobs, today):
    points, detail = 0, []

    def add(n, label):
        nonlocal points
        points += n
        detail.append(f"{label} +{n}")

    others = {t for t in (det.get("autres_outils") or "").split(", ") if t}
    saas_hints = [h for h in (det.get("indices_saas") or "").split(", ") if h]
    hubspot = det.get("hubspot") in {"Oui", "Probable"} or "hubspot" in rec["technologies"].lower()
    titles = jobs.get("titles", [])
    revops_jobs = [t for t in titles if REVOPS_TITLE.search(t)]
    sales_jobs = [t for t in titles if SALES_TITLE.search(t) and not REVOPS_TITLE.search(t)]
    raised = parse_date(rec["last_raised"])

    if SAAS_INDUSTRY.search(rec["industry"]) or "saas" in rec["keywords"].lower() or len(saas_hints) >= 2:
        add(3, "SaaS/tech")
    employees = parse_int(rec["employees"])
    if employees is not None and 20 <= employees <= 200:
        add(2, "20-200 salariés")
    if hubspot:
        add(2, "HubSpot")
    if rec["state"].lower() in {"ontario", "on"}:
        add(2, "Ontario")
    elif rec["country"].lower() == "canada" or (not rec["country"] and rec["domain"].endswith(".ca")):
        add(1, "Canada")
    if revops_jobs:
        add(5, "Offre RevOps/Ops ouverte")
    if raised and (today - raised).days <= 365:
        add(3, "Levée < 12 mois")
    if len(sales_jobs) >= 3:
        add(2, "3+ offres sales/marketing")
    if hubspot and others & COMPETITOR_CRM:
        add(3, "Autre CRM visible")

    angles = []
    if revops_jobs:
        angles.append(f"Recrute « {revops_jobs[0]} » : intérim RevOps pendant le recrutement")
    if others & COMPETITOR_CRM:
        crm = ", ".join(sorted(others & COMPETITOR_CRM))
        angles.append(f"{crm} visible à côté de HubSpot : migration ou consolidation" if hubspot else f"Sur {crm} : audit ou migration vers HubSpot")
    if hubspot and others & {"Mailchimp", "ActiveCampaign"}:
        angles.append("Emailing hors HubSpot : consolider")
    if hubspot and "Calendly" in others and "lien-meetings" not in (det.get("signaux_hubspot") or ""):
        angles.append("RDV via Calendly hors CRM : HubSpot Meetings + suivi pipeline")
    if hubspot and others & {"Intercom", "Drift"}:
        angles.append("Conversations clients hors HubSpot")
    if hubspot and "Typeform" in others:
        angles.append("Leads Typeform mal reliés au CRM")
    if len(sales_jobs) >= 3:
        angles.append(f"{len(sales_jobs)} offres sales/marketing : structurer process et reporting avant l'arrivée des recrues")
    if raised and (today - raised).days <= 365:
        angles.append("Levée récente : structurer le RevOps pour la croissance")

    tier = "A" if points >= 12 else "B" if points >= 8 else "C"
    return {
        "tier": tier,
        "score": points,
        "detail_score": "; ".join(detail),
        "angle_suggere": " | ".join(angles[:2]),
        "offres_revops": " ; ".join(revops_jobs[:3]),
        "nb_offres_sales_marketing": len(sales_jobs),
        "hubspot_site": det.get("hubspot", ""),
        "hubspot_apollo": "Oui" if "hubspot" in rec["technologies"].lower() else "",
        "derniere_levee": raised.isoformat() if raised else "",
    }


RANK_FIELDS = [
    "Rang", "Tier", "Score", "Détail score", "Angle suggéré", "Offres RevOps ouvertes",
    "Nb offres sales/marketing", "HubSpot (site)", "Autres outils",
]


def write_ranked_source(path, rows):
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        original_fields = [c for c in reader.fieldnames or [] if c not in RANK_FIELDS]
        source = list(reader)
    website_col = column_map(original_fields)["website"]
    scored_by_domain = {r["domaine"]: r for r in rows}
    position = {r["domaine"]: i for i, r in enumerate(rows)}

    ranked = []
    for src in source:
        domain = normalize(src.get(website_col) or "")
        scored = scored_by_domain.get(domain)
        if scored is None:
            extra = {"Tier": "Non scoré : site à trouver"}
        else:
            extra = {
                "Tier": scored["tier"],
                "Score": scored.get("score", ""),
                "Détail score": scored.get("detail_score", ""),
                "Angle suggéré": scored.get("angle_suggere", ""),
                "Offres RevOps ouvertes": scored.get("offres_revops", ""),
                "Nb offres sales/marketing": scored.get("nb_offres_sales_marketing", ""),
                "HubSpot (site)": scored.get("hubspot_site", ""),
                "Autres outils": scored.get("autres_outils", ""),
            }
        row = {**{k: src.get(k, "") for k in original_fields}, **extra}
        ranked.append((position.get(domain, len(rows)), row))
    ranked.sort(key=lambda item: item[0])

    rank = 0
    for _, row in ranked:
        if row["Tier"] in TIER_ORDER:
            rank += 1
            row["Rang"] = rank
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=RANK_FIELDS + original_fields, restval="")
        writer.writeheader()
        writer.writerows(row for _, row in ranked)
    return rank


def exclusions(records, applied):
    """Sépare les entreprises à analyser de celles à écarter (candidature CDI, moins de 10 salariés)."""
    excluded, candidates = {}, []
    for rec in records:
        employees = parse_int(rec["employees"])
        if norm_company(rec["company"]) in applied:
            excluded[rec["domain"]] = "Exclu : candidature CDI"
        elif employees is not None and employees < 10:
            excluded[rec["domain"]] = "Exclu : moins de 10 salariés"
        else:
            candidates.append(rec)
    return candidates, excluded


def score_records(records, applied, cache_dir=CACHE, workers=24, timeout=10, ats_min=5, today=None, refresh=False):
    """Pipeline de scoring : exclusions -> détection des sites -> offres d'emploi -> score. Renvoie les lignes triées."""
    today = today or date.today()
    candidates, excluded = exclusions(records, applied)
    print(f"{len(excluded)} exclues · {len(candidates)} à analyser", flush=True)

    cache_dir = Path(cache_dir)
    det_cache = Cache(cache_dir / "detection.jsonl", 0 if refresh else DETECTION_MAX_AGE_DAYS)
    run_stage("Détection des sites", [r["domain"] for r in candidates],
              lambda d: analyze(d, timeout), det_cache, workers)
    detections = det_cache.data()

    by_domain = {r["domain"]: r for r in candidates}
    ats_targets = [r["domain"] for r in candidates
                   if score(r, detections.get(r["domain"], {}), {}, today)["score"] >= ats_min]
    ats_cache = Cache(cache_dir / "ats.jsonl", 0 if refresh else ATS_MAX_AGE_DAYS)
    run_stage("Offres d'emploi (ATS)", ats_targets,
              lambda d: fetch_jobs(by_domain[d], detections.get(d, {}), timeout), ats_cache, workers)
    jobs = ats_cache.data()

    rows = []
    for rec in records:
        det = detections.get(rec["domain"], {})
        base = {
            "entreprise": rec["company"], "domaine": rec["domain"], "employes": rec["employees"],
            "secteur": rec["industry"], "ville": rec["city"], "province": rec["state"], "pays": rec["country"],
            "linkedin": rec["linkedin"], "autres_outils": det.get("autres_outils", ""),
            "indices_saas": det.get("indices_saas", ""), "erreur_site": det.get("erreur", ""),
        }
        if rec["domain"] in excluded:
            rows.append({**base, "tier": excluded[rec["domain"]], "score": ""})
            continue
        job_data = jobs.get(rec["domain"], {})
        rows.append({**base, **score(rec, det, job_data, today), "ats": job_data.get("ats", "")})
    rows.sort(key=lambda r: (TIER_ORDER.get(r["tier"], 3), -(r["score"] or 0), r["entreprise"]))
    return rows, excluded


def load_applied(path, required=False):
    """Charge le tracker. Sans fichier : avertissement bien visible (ou arrêt si required)."""
    try:
        applied = applied_companies(path)
        print(f"Tracker de candidatures : {len(applied)} entreprises exclues d'office ({path})", flush=True)
        return applied
    except FileNotFoundError as exc:
        if required:
            sys.exit(f"ERREUR : {exc}\n  → indique le bon chemin avec --tracker, ou dans config.json (clé job_tracker)")
        print(f"ATTENTION : {exc}\n  → les entreprises où tu as déjà postulé ne seront PAS exclues.", flush=True)
        return set()


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Score un export d'entreprises (Apollo ou autre) selon la grille ICP.")
    parser.add_argument("export", help="CSV d'entreprises avec au moins une colonne site web")
    parser.add_argument("--out", default=str(OUTPUT / "prospects-scores.csv"))
    parser.add_argument("--cache-dir", default=str(CACHE))
    parser.add_argument("--candidatures", "--tracker", dest="tracker", default=None,
                        help="job_search_tracker.csv, pour exclure les entreprises où tu as postulé (défaut : config.json ou ../ai-job-search)")
    parser.add_argument("--workers", type=int, default=24)
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--ats-min", type=int, default=5, help="score minimum avant d'aller chercher les offres d'emploi")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--refresh", action="store_true", help="ignore le cache et réanalyse tout")
    parser.add_argument("--rank-source", action="store_true", help="réécrit le fichier d'entrée trié par rang, avec les colonnes de score (toutes les colonnes existantes sont gardées)")
    args = parser.parse_args()

    try:
        records, without_site, missing = read_export(args.export)
    except ValueError as exc:
        sys.exit(str(exc))
    print(f"{len(records)} entreprises uniques · {without_site} sans site ignorées", flush=True)
    if missing:
        print(f"Colonnes absentes (critères correspondants non scorés) : {', '.join(missing)}", flush=True)

    applied = load_applied(args.tracker or default_tracker(), required=bool(args.tracker))
    rows, excluded = score_records(records, applied, args.cache_dir, args.workers, args.timeout, args.ats_min, refresh=args.refresh)

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS, restval="")
        writer.writeheader()
        writer.writerows(rows)
    if args.rank_source:
        ranked_count = write_ranked_source(args.export, rows)
        print(f"{args.export} réécrit : {ranked_count} entreprises classées, colonnes existantes conservées", flush=True)

    tiers = {t: sum(r["tier"] == t for r in rows) for t in TIER_ORDER}
    print(f"\nTier A : {tiers['A']} · Tier B : {tiers['B']} · Tier C : {tiers['C']} · Exclues : {len(excluded)}")
    print(f"\nTop {args.top} :")
    for r in rows[: args.top]:
        if r["tier"] not in TIER_ORDER:
            break
        print(f"  {r['tier']} {r['score']:>2}  {r['entreprise'][:28]:<28} {r['domaine']:<28} {r['angle_suggere'][:80]}")
    print(f"\n→ {args.out}")


if __name__ == "__main__":
    main()
