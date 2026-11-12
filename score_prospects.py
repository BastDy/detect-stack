"""Score un export d'entreprises (Apollo, BetaKit, YC…) selon la grille ICP et propose un angle d'approche."""
import argparse
import csv
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from common import (  # noqa: F401  (LEADERSHIP_TITLE réexporté)
    CACHE, CITY_PROVINCE, HERE, LEADERSHIP_TITLE, OUTPUT, PROVINCE_CODES, REVOPS_TITLE, SALES_TITLE, Cache, fetch,
    job_category, load_config, norm_company, normalize, run_stage,
)
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
COMPETITOR_CRM = {"Salesforce Pardot", "Salesforce Web-to-Lead", "Marketo", "Pipedrive", "Zoho"}


def _s(value):
    return value.strip() if isinstance(value, str) else (str(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else "")


def _iso_date(value):
    """Timestamp ms/s, ISO 8601 ou 'YYYY-MM-DD' -> 'YYYY-MM-DD' ; "" si illisible."""
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            ts = value / 1000 if value > 1e11 else value
            return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
        if isinstance(value, str) and re.match(r"\d{4}-\d{2}-\d{2}", value.strip()):
            return value.strip()[:10]
    except (ValueError, OverflowError, OSError):
        pass
    return ""


def _posting(pid, title, location, url, published):
    title = _s(title)
    if not title:
        return None
    return {
        "posting_id": _s(pid) or norm_company(title) or title.lower(),
        "title": title, "location": _s(location), "url": _s(url), "published_at": _iso_date(published),
        "category": job_category(title),
    }


def _items(payload, key="jobs"):
    items = payload.get(key) if isinstance(payload, dict) else payload
    return [j for j in items if isinstance(j, dict)] if isinstance(items, list) else []


def _dict(value):
    return value if isinstance(value, dict) else {}


def _greenhouse(d):
    # first_published n'existe que sur certains boards ; updated_at n'est pas une date de publication.
    return [_posting(j.get("id"), j.get("title"), _dict(j.get("location")).get("name"), j.get("absolute_url"), j.get("first_published"))
            for j in _items(d)]


def _lever(d):
    return [_posting(j.get("id"), j.get("text"), _dict(j.get("categories")).get("location"), j.get("hostedUrl"), j.get("createdAt"))
            for j in _items(d, None)]


def _ashby(d):
    return [_posting(j.get("id"), j.get("title"), j.get("location"), j.get("jobUrl"), j.get("publishedAt")) for j in _items(d)]


def _workable(d):
    out = []
    for j in _items(d):
        place = ", ".join(x for x in (_s(j.get("city")), _s(j.get("country"))) if x)
        out.append(_posting(j.get("shortcode") or j.get("id"), j.get("title"), place, j.get("url"),
                            j.get("published_on") or j.get("created_at")))
    return out


def _postings(extractor, payload):
    return [p for p in extractor(payload) if p]


ATS_API = {
    "greenhouse": ("https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", _greenhouse),
    "lever": ("https://api.lever.co/v0/postings/{slug}?mode=json&limit=100", _lever),
    "ashby": ("https://api.ashbyhq.com/posting-api/job-board/{slug}", _ashby),
    "workable": ("https://apply.workable.com/api/v1/widget/accounts/{slug}", _workable),
}
TIER_ORDER = {"A": 0, "B": 1, "C": 2}
# Villes de CITY_PROVINCE qui existent aussi hors du Canada (London UK, Cambridge MA, Victoria AU…) :
# on n'en déduit la province que s'il y a une preuve canadienne.
AMBIGUOUS_CITIES = {
    "london", "cambridge", "richmond", "richmond hill", "victoria", "surrey", "kingston", "hamilton", "burlington",
    "waterloo", "halifax", "dartmouth", "laval", "scarborough", "brampton", "pickering", "whitby", "markham",
}
# Grille de score par défaut. config.json peut en surcharger une partie sous la clé "scoring",
# avec la même structure (ex. {"scoring": {"points": {"funding": 4}, "tiers": {"A": 13}}}).
DEFAULT_SCORING = {
    "points": {"saas": 3, "size": 2, "hubspot": 2, "ontario": 2, "canada": 1,
               "revops_job": 5, "funding": 3, "sales_jobs": 2, "other_crm": 3},
    "size": {"min": 20, "max": 200, "exclude_below": 10},  # fourchette ICP ; exclusion sous exclude_below salariés
    "signals": {"sales_jobs_min": 3, "funding_days": 365},
    "tiers": {"A": 12, "B": 8},  # score minimum de chaque tier, C en dessous
}
PROVINCES = {name.lower(): name for name in PROVINCE_CODES.values()} | {code.lower(): name for code, name in PROVINCE_CODES.items()}
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
        # Apollo "Location (Canada)" : le nom de colonne lui-même garantit que la ville est canadienne.
        canadian_source = "canada" in (cols["city"] or "").lower()
        for row in reader:
            rec = {key: (row.get(col) or "").strip() if col else "" for key, col in cols.items()}
            rec["domain"] = normalize(rec["website"])
            if not rec["domain"]:
                without_site += 1
                continue
            fill_location(rec, canadian_source=canadian_source)
            records.setdefault(rec["domain"], rec)
    missing = [key for key, col in cols.items() if col is None]
    return list(records.values()), without_site, missing


def province_of(value):
    """'ON', 'ontario', 'Ontario, Canada' -> 'Ontario' ; None si ce n'est pas une province canadienne."""
    return PROVINCES.get(value.split(",")[0].strip().lower()) if value else None


def fill_location(rec, canadian_source=False):
    """Déduit province et pays de la ville quand l'export ne les donne pas.

    Une ville ambiguë (London, Cambridge…) n'est rattachée au Canada qu'avec une preuve : pays Canada,
    domaine .ca, state ou suffixe de ville ("London, ON") qui est une province, ou source canadienne.
    Un state / pays / suffixe étranger ("England", "CA", "Cambridge, MA") bloque toute déduction.
    """
    city, _, suffix = (part.strip() for part in rec["city"].partition(","))
    province = CITY_PROVINCE.get(city.lower())
    country, state = rec["country"].strip().lower(), rec["state"].strip()
    suffix_province = province_of(suffix)
    if not province:
        return
    # Indice étranger explicite : pays non canadien, state qui n'est pas une province, "Cambridge, MA".
    foreign_country = country and country != "canada"
    foreign_state = state and not province_of(state)
    foreign_suffix = suffix and not suffix_province and suffix.lower() != "canada"
    if foreign_country or foreign_state or foreign_suffix:
        return
    canadian = (country == "canada" or rec["domain"].endswith(".ca") or canadian_source
                or state or suffix)  # à ce stade, un state ou un suffixe non vide est forcément canadien
    if city.lower() in AMBIGUOUS_CITIES and not canadian:
        return
    if not state:
        rec["state"] = suffix_province or province
    if not country:
        rec["country"] = "Canada"


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


def fetch_text(url, timeout):
    return fetch(url, timeout, accept="application/json", max_bytes=5_000_000)[1]


def fetch_json(url, timeout):
    return json.loads(fetch_text(url, timeout))


def scoring_config():
    """Grille de score : DEFAULT_SCORING surchargée par la clé "scoring" de config.json.

    Lève ValueError sur une clé inconnue (faute de frappe) ou une valeur non numérique.
    """
    custom = load_config().get("scoring", {})
    if not isinstance(custom, dict):
        raise ValueError("config.json : \"scoring\" doit être un objet {...}")
    grid = {section: dict(values) for section, values in DEFAULT_SCORING.items()}
    for section, values in custom.items():
        if section not in grid:
            raise ValueError(f"config.json : section de scoring inconnue « {section} » "
                             f"(sections possibles : {', '.join(grid)})")
        if not isinstance(values, dict):
            raise ValueError(f"config.json : scoring.{section} doit être un objet {{...}}")
        for key, value in values.items():
            if key not in grid[section]:
                raise ValueError(f"config.json : clé inconnue « scoring.{section}.{key} » "
                                 f"(clés possibles : {', '.join(grid[section])})")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"config.json : scoring.{section}.{key} doit être un nombre (lu : {value!r})")
            grid[section][key] = value
    return grid


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
    """Offres du board ATS de l'entreprise.

    Renvoie {"ats": libellé affiché, "titles", "postings", "board": "provider:slug", "ok": bool} :
      - board lu (même vide)          -> board renseigné, ok True, postings = offres lues ;
      - lien du site mais API en échec -> board renseigné, ok False, aucune offre (rien à conclure) ;
      - aucun board (ni lien, ni slug deviné vérifié) -> board "", ok True.
    """
    candidates, source = ats_candidates(rec, detection)
    failed = ""
    for provider, slug in candidates:
        url, extract = ATS_API[provider]
        try:
            raw = fetch_text(url.format(slug=slug), timeout)
            payload = json.loads(raw)
            postings = _postings(extract, payload)[:300]
            titles = [p["title"] for p in postings]
        except Exception:
            if source == "lien du site":
                failed = failed or f"{provider}:{slug}"
            continue
        board = f"{provider}:{slug}"
        if source == "lien du site":
            return {"ats": f"{board} ({source})", "titles": titles, "postings": postings, "board": board, "ok": True}
        if titles and board_belongs_to(rec, provider, slug, raw, payload, timeout):
            return {"ats": f"{board} (slug deviné, vérifié)", "titles": titles, "postings": postings,
                    "board": board, "ok": True}
    return {"ats": "", "titles": [], "board": failed, "ok": not failed}


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


def score(rec, det, jobs, today, grid=None):
    grid = grid or DEFAULT_SCORING
    pts, size, signals, tiers = grid["points"], grid["size"], grid["signals"], grid["tiers"]
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
    recent_raise = bool(raised) and (today - raised).days <= signals["funding_days"]
    many_sales_jobs = len(sales_jobs) >= signals["sales_jobs_min"]
    window = "12 mois" if signals["funding_days"] == 365 else f"{signals['funding_days']} jours"

    if SAAS_INDUSTRY.search(rec["industry"]) or "saas" in rec["keywords"].lower() or len(saas_hints) >= 2:
        add(pts["saas"], "SaaS/tech")
    employees = parse_int(rec["employees"])
    if employees is not None and size["min"] <= employees <= size["max"]:
        add(pts["size"], f"{size['min']}-{size['max']} salariés")
    if hubspot:
        add(pts["hubspot"], "HubSpot")
    if rec["state"].lower() in {"ontario", "on"}:
        add(pts["ontario"], "Ontario")
    elif rec["country"].lower() == "canada" or (not rec["country"] and rec["domain"].endswith(".ca")):
        add(pts["canada"], "Canada")
    if revops_jobs:
        add(pts["revops_job"], "Offre RevOps/Ops ouverte")
    if recent_raise:
        add(pts["funding"], f"Levée < {window}")
    if many_sales_jobs:
        add(pts["sales_jobs"], f"{signals['sales_jobs_min']}+ offres sales/marketing")
    if hubspot and others & COMPETITOR_CRM:
        add(pts["other_crm"], "Autre CRM visible")

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
    if many_sales_jobs:
        angles.append(f"{len(sales_jobs)} offres sales/marketing : structurer process et reporting avant l'arrivée des recrues")
    if recent_raise:
        angles.append("Levée récente : structurer le RevOps pour la croissance")

    tier = "A" if points >= tiers["A"] else "B" if points >= tiers["B"] else "C"
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


def exclusions(records, applied, grid=None):
    """Sépare les entreprises à analyser de celles à écarter (candidature CDI, trop peu de salariés)."""
    min_size = (grid or DEFAULT_SCORING)["size"]["exclude_below"]
    excluded, candidates = {}, []
    for rec in records:
        employees = parse_int(rec["employees"])
        if norm_company(rec["company"]) in applied:
            excluded[rec["domain"]] = "Exclu : candidature CDI"
        elif employees is not None and employees < min_size:
            excluded[rec["domain"]] = f"Exclu : moins de {min_size} salariés"
        else:
            candidates.append(rec)
    return candidates, excluded


def score_records(records, applied, cache_dir=CACHE, workers=24, timeout=10, ats_min=None, today=None, refresh=False,
                  stores=None):
    """Pipeline de scoring : exclusions -> détection des sites -> offres d'emploi -> score. Renvoie les lignes triées.

    stores : {"detection": ..., "ats": ...} pour remplacer les caches JSONL de cache_dir par un autre stockage
    exposant la même interface que Cache (stale / get / put / writer / compact / data), ex. pipeline.HistoryStore.
    La fraîcheur (et --refresh) est alors l'affaire du store.
    """
    today = today or date.today()
    try:
        grid = scoring_config()
    except ValueError as exc:
        sys.exit(f"ERREUR : {exc}")
    candidates, excluded = exclusions(records, applied, grid)
    print(f"{len(excluded)} exclues · {len(candidates)} à analyser", flush=True)

    cache_dir = Path(cache_dir)
    stores = stores or {}
    det_cache = stores.get("detection") or Cache(cache_dir / "detection.jsonl", 0 if refresh else DETECTION_MAX_AGE_DAYS)
    run_stage("Détection des sites", [r["domain"] for r in candidates],
              lambda d: analyze(d, timeout), det_cache, workers)
    detections = det_cache.data()

    by_domain = {r["domain"]: r for r in candidates}
    # Sans surcharge ats_min : on cherche les offres si l'entreprise peut atteindre le tier B grâce à elles.
    pts = grid["points"]
    threshold = ats_min if ats_min is not None else grid["tiers"]["B"] - pts["revops_job"] - pts["sales_jobs"]
    ats_targets = [r["domain"] for r in candidates
                   if score(r, detections.get(r["domain"], {}), {}, today, grid)["score"] >= threshold]
    ats_cache = stores.get("ats") or Cache(cache_dir / "ats.jsonl", 0 if refresh else ATS_MAX_AGE_DAYS)
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
        rows.append({**base, **score(rec, det, job_data, today, grid), "ats": job_data.get("ats", "")})
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
    parser.add_argument("--ats-min", type=int, default=None,
                        help="score minimum (sans offres) pour aller chercher les offres d'emploi. Défaut : automatique, "
                             "toute entreprise pouvant atteindre le tier B grâce aux offres (tier B - points revops_job - points sales_jobs)")
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
