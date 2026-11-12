"""Pipeline complet de prospection, en une commande.

    python pipeline.py

Étapes :
  1. Sources     BetaKit (levées 12 mois) + YC Canada + tous les CSV posés dans data/inputs/ (exports Apollo…)
  2. Fusion      dédoublonnage par domaine, les infos des différentes sources se complètent
  3. Exclusions  entreprises où tu as déjà postulé (tracker ai-job-search), moins de 10 salariés
     -> chaque entreprise (et sa dernière levée) est enregistrée dans l'historique data/history.db
  4. Détection   stack du site (HubSpot, CRM concurrents, outils), relue selon l'historique :
                 tier A/B chaque jour, sinon tous les 30 jours, site en erreur le lendemain
  5. Offres      postes ouverts via Greenhouse / Lever / Ashby / Workable (pré-filtre : seules les entreprises
                 pouvant atteindre le tier B) : tier A/B chaque jour, sinon tous les 7 jours, échec le lendemain
  6. Score       grille ICP -> tier A / B / C + angle d'approche ; score enregistré dans l'historique
  7. LinkedIn    page entreprise pour les tiers A et B : cache 90 jours
  8. Sortie      data/output/prospects-AAAA-MM-JJ.csv (colonne signaux_recents = changements des 30 derniers
                 jours) + prospects-latest.csv + resume-AAAA-MM-JJ.md (section « Ce qui a changé »)

Chaque lecture (site, board d'offres) est enregistrée dans data/history.db (voir SCHEMA.md) ; les changements
entre deux lectures deviennent des événements (outil installé, offre ouverte, levée, changement de tier…).
Un domaine non dû n'est pas relu : son dernier relevé en base sert au score. --refresh relit tout.
Les colonnes que tu ajoutes à la main dans prospects-latest.csv (Notes, Statut…) sont conservées d'un run à l'autre.
"""
import argparse
import contextlib
import csv
import re
import shutil
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import betakit_funding
import db
import find_linkedin
import yc_companies
from common import CACHE, INPUTS, OUTPUT, Cache, is_error, job_category, normalize, run_stage
from score_prospects import (
    OUTPUT_FIELDS, TIER_ORDER, column_map, default_tracker, exclusions, load_applied, parse_date, read_export,
    score_records, scoring_config,
)

LINKEDIN_MAX_AGE_DAYS = 90
SIGNALS_DAYS = 30   # fenêtre de la colonne signaux_recents
SIGNALS_MAX = 3
_BASE_FIELDS = OUTPUT_FIELDS[:2] + ["nouveau"] + OUTPUT_FIELDS[2:]
_AFTER_ANGLE = _BASE_FIELDS.index("angle_suggere") + 1
PIPELINE_FIELDS = _BASE_FIELDS[:_AFTER_ANGLE] + ["signaux_recents"] + _BASE_FIELDS[_AFTER_ANGLE:] + ["sources", "vu_depuis"]
LATEST = OUTPUT / "prospects-latest.csv"


# ---------- Historique : fraîcheur des relectures ----------

class HistoryStore:
    """Interface de common.Cache (stale / get / put / writer / compact / data) au-dessus de data/history.db.

    Remplace, dans le pipeline, les caches JSONL detection / ats passés à score_records :
      - stale()  = domaines dus selon db.due_for_scan / db.due_for_jobs (tout, avec --refresh) ;
      - put()    = enregistre la lecture dans la base (record_scan, record_postings…), commit immédiat ;
      - data()   = lecture de ce run, sinon dernier relevé en base pour les domaines non dus, remis au format
                   de detect_stack.analyze / fetch_jobs, pour que score() reçoive les mêmes entrées qu'avec le cache.
    put() est appelé par run_stage depuis le thread principal : la connexion SQLite n'est jamais partagée.
    """

    def __init__(self, conn, kind, run_id, today, domains, refresh=False):
        if kind not in {"scan", "jobs"}:
            raise ValueError(f"HistoryStore : type inconnu {kind!r}")
        self.conn, self.kind, self.run_id, self.today, self.refresh = conn, kind, run_id, today, refresh
        self.domains = list(dict.fromkeys(domains))
        self.done = {}  # domaine -> résultat lu pendant ce run

    def _due(self, domains):
        if self.refresh:
            return list(domains)
        due = db.due_for_scan if self.kind == "scan" else db.due_for_jobs
        return due(self.conn, list(domains), self.today)

    def stale(self, domains):
        return [d for d in self._due(domains) if d not in self.done]

    def get(self, domain):
        if domain in self.done:
            return self.done[domain]
        return None if self._due([domain]) else self._stored(domain)

    def data(self):
        due = set(self._due(self.domains))
        out = {}
        for domain in self.domains:
            value = self.done.get(domain)
            if value is None and domain not in due:
                value = self._stored(domain)
            if value is not None:
                out[domain] = value
        return out

    def writer(self):
        return contextlib.nullcontext()

    def compact(self):
        pass

    def put(self, domain, data, out=None):
        self.done[domain] = data
        if self.kind == "scan":
            db.record_scan(self.conn, domain, data, self.run_id, f"{self.today.isoformat()}T{datetime.now():%H:%M:%S}")
            return
        board = data.get("board") or ""
        if not board:
            # Aucun board (ok) ou fetch_jobs a planté ({"erreur": …}) : pas de record_postings, '' n'est pas un board.
            db.record_jobs_lookup(self.conn, domain, self.run_id, self.today, ok="erreur" not in data)
        elif not data.get("ok", True):
            db.record_postings(self.conn, domain, board, None, self.run_id, self.today)  # lien du site, API en échec
        else:
            db.record_postings(self.conn, domain, board, data.get("postings") or [], self.run_id, self.today)

    def _stored(self, domain):
        return stored_detection(self.conn, domain) if self.kind == "scan" else stored_jobs(self.conn, domain)


def stored_detection(conn, domain):
    """Dernière lecture du site en base, au format de detect_stack.analyze ; None si jamais lu."""
    s = db.last_scan(conn, domain)
    if s is None:
        return None
    return {
        "domaine": domain, "hubspot": s["hubspot"] or "", "portal_id": ";".join(s["portal_ids"] or []),
        "region": s["region"] or "", "signaux_hubspot": ", ".join(s["hubspot_signals"] or []),
        "autres_outils": ", ".join(s["tools"] or []), "indices_saas": ", ".join(s["saas_hints"] or []),
        "ats": ";".join(s["ats_links"] or []), "pages_analysees": s["pages_read"], "erreur": s["error"] or "",
    }


def stored_jobs(conn, domain):
    """Dernier relevé d'offres réussi, au format de fetch_jobs (titres vus à ce relevé) ; None si aucun."""
    read = db.last_job_read(conn, domain, ok_only=True)
    if read is None:
        return None
    board = read["ats"]
    if not board:
        return {"ats": "", "titles": [], "board": "", "ok": True}
    seen = [p for p in db.open_postings(conn, domain, board) if p["last_seen"] == read["day"]]
    scan = db.last_scan(conn, domain) or {}
    source = "lien du site" if board in (scan.get("ats_links") or []) else "slug deviné, vérifié"
    return {"ats": f"{board} ({source})", "titles": [p["title"] for p in seen], "board": board, "ok": True,
            "postings": [{k: p[k] for k in ("posting_id", "title", "category", "location", "url", "published_at")}
                         for p in seen]}


# ---------- 1. Sources ----------

def collect_sources(args, warnings):
    """Renvoie [(nom, chemin CSV)]. Une source en panne n'arrête pas le run : on reprend sa dernière version."""
    sources = []
    builtin = []
    if not args.no_betakit:
        builtin.append(("betakit", betakit_funding.DEFAULT_OUT, lambda: betakit_funding.run(12, betakit_funding.DEFAULT_OUT, args.refresh_sources)))
    if not args.no_yc:
        builtin.append(("yc", yc_companies.DEFAULT_OUT, lambda: yc_companies.run(yc_companies.DEFAULT_OUT, args.refresh_sources)))
    for name, path, build in builtin:
        print(f"\n== Source {name} ==", flush=True)
        try:
            sources.append((name, build()))
        except Exception as exc:
            msg = f"Source {name} indisponible ({type(exc).__name__}: {exc})"
            if Path(path).exists():
                warnings.append(msg + " : dernière version utilisée")
                sources.append((name, Path(path)))
            else:
                warnings.append(msg + " : ignorée")
            print("  " + warnings[-1], flush=True)

    inputs = Path(args.inputs)
    inputs.mkdir(parents=True, exist_ok=True)
    exports = sorted(inputs.glob("*.csv"))
    for path in exports:
        sources.append((path.stem, path))
    if not exports:
        print(f"\n(Aucun export dans {inputs} : pose-y tes exports Apollo/Clay en CSV pour les inclure.)", flush=True)
    return sources


# ---------- 2. Fusion ----------

_UNITS = {"thousand": 1e3, "k": 1e3, "million": 1e6, "m": 1e6, "billion": 1e9, "b": 1e9}


def parse_amount(text):
    """'10 million' / '2.5 b' / '750,000' (colonne Montant de BetaKit) -> 10000000.0… ; None si illisible."""
    match = re.match(r"\s*([\d.,]+)\s*([a-z]*)", (text or "").lower())
    if not match:
        return None
    try:
        value = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    return value * _UNITS.get(match.group(2), 1)


def funding_details(path):
    """Montant / devise / tour / article des CSV qui en ont (BetaKit), par domaine ; {} sinon."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if "Montant" not in (reader.fieldnames or []):
            return {}
        website = column_map(reader.fieldnames)["website"]
        details = {}
        for row in reader:
            domain = normalize(row.get(website) or "")
            announced = parse_date(row.get("Last Raised At"))
            if domain and announced:
                details.setdefault(domain, {"announced_on": announced, "amount": parse_amount(row.get("Montant")),
                                            "currency": (row.get("Devise") or "").strip() or None,
                                            "round": (row.get("Tour") or "").strip(),
                                            "url": (row.get("Source") or "").strip() or None})
    return details


def merge_sources(sources, warnings):
    merged = {}
    print("\n== Fusion ==", flush=True)
    for name, path in sources:
        try:
            records, without_site, _ = read_export(path)
            details = funding_details(path)
        except (ValueError, OSError) as exc:
            warnings.append(f"{name} ignoré : {exc}")
            print("  " + warnings[-1], flush=True)
            continue
        print(f"  {name:<28} {len(records):>4} entreprises · {without_site} sans site", flush=True)
        for rec in records:
            if rec["domain"] in details:
                rec["funding"] = details[rec["domain"]]
            current = merged.get(rec["domain"])
            if current is None:
                merged[rec["domain"]] = {**rec, "sources": [name]}
                continue
            current["sources"].append(name)
            for key, value in rec.items():
                if key == "last_raised":
                    if (parse_date(value) or date.min) > (parse_date(current[key]) or date.min):
                        current[key] = value
                        current["funding"] = rec.get("funding")  # les détails suivent la levée retenue
                elif value and not current.get(key):
                    current[key] = value
    for rec in merged.values():
        rec["sources"] = "; ".join(dict.fromkeys(rec["sources"]))
    print(f"  → {len(merged)} entreprises uniques", flush=True)
    return list(merged.values())


# ---------- 7. LinkedIn ----------

def add_linkedin(rows, workers, timeout, refresh):
    targets = [r["domaine"] for r in rows if r["tier"] in {"A", "B"} and not r.get("linkedin")]
    cache = Cache(CACHE / "linkedin.jsonl", 0 if refresh else LINKEDIN_MAX_AGE_DAYS)
    run_stage("LinkedIn (tiers A et B)", targets, lambda d: find_linkedin.analyze(d, timeout), cache, workers)
    found = cache.data()
    for r in rows:
        if not r.get("linkedin"):
            r["linkedin"] = found.get(r["domaine"], {}).get("linkedin", "")


# ---------- Historique SQLite (data/history.db) ----------

def record_companies(conn, records, excluded, run_id, today):
    """Fiche entreprise (avec la raison d'exclusion, effacée si elle ne s'applique plus) et dernière levée connue."""
    for rec in records:
        db.upsert_company(conn, {**rec, "excluded_reason": excluded.get(rec["domain"], "")}, today)
        announced = parse_date(rec.get("last_raised"))
        if not announced:
            continue
        fund = rec.get("funding") or {}
        if fund.get("announced_on") != announced:
            fund = {}  # détails d'une autre levée (ou inconnus) : on n'enregistre que la date
        db.record_funding(conn, rec["domain"], announced, fund.get("amount"), fund.get("currency"), fund.get("round"),
                          fund.get("url"), run_id)


def _event_text(event):
    """Événement -> texte court en français, ex. 'HubSpot installé', 'offre RevOps ouverte : RevOps Manager'."""
    kind, detail = event["type"], event["detail"] or ""
    if kind == "posting_opened":
        label = {"revops": "offre RevOps", "leadership": "offre de direction", "sales": "offre sales/marketing"}
        return f"{label.get(job_category(detail), 'offre')} ouverte : {detail}"
    templates = {
        "tool_added": "{} installé", "tool_removed": "{} retiré", "second_portal": "{}",
        "posting_closed": "offre fermée : {}", "posting_stale": "offre RevOps non pourvue : {}",
        "leadership_hire": "recrute à la direction : {}", "hiring_spike": "hausse des recrutements ({})",
        "funding": "levée de fonds ({})", "tier_up": "monte {}", "tier_down": "descend {}",
    }
    if kind == "funding" and detail == "levée":
        return "levée de fonds"
    return templates.get(kind, kind + " : {}").format(detail)


def recent_signals(conn, domain, today):
    """Les SIGNALS_MAX derniers événements des SIGNALS_DAYS derniers jours, du plus ancien au plus récent."""
    events = db.recent_events(conn, domain, today - timedelta(days=SIGNALS_DAYS), limit=SIGNALS_MAX)
    return " · ".join(f"{e['day']} {_event_text(e)}" for e in reversed(events))


CHANGE_GROUPS = {
    "funding": "Levées de fonds", "tool_added": "Outils installés", "tool_removed": "Outils retirés",
    "second_portal": "Plusieurs portals HubSpot", "posting_opened": "Offres ouvertes",
    "leadership_hire": "Recrutements à la direction", "hiring_spike": "Hausse des recrutements sales",
    "posting_stale": "Offres RevOps non pourvues", "posting_closed": "Offres fermées",
    "tier_up": "Montées de tier", "tier_down": "Baisses de tier",
}


def changes_section(changes):
    """Section « Ce qui a changé » du résumé : événements du run, groupés par type, avec le nom de l'entreprise."""
    lines = ["## Ce qui a changé", ""]
    if not changes:
        return lines + ["Aucun changement détecté depuis le run précédent.", ""]
    for kind in list(CHANGE_GROUPS) + sorted({e["type"] for e in changes} - set(CHANGE_GROUPS)):
        group = [e for e in changes if e["type"] == kind]
        if group:
            lines += [f"### {CHANGE_GROUPS.get(kind, kind)} ({len(group)})", ""]
            lines += [f"- {e['name'] or e['domain']} ({e['domain']}) : {e['detail'] or ''}"
                      + (f" · {e['day']}" if kind == "funding" else "") for e in group]
            lines.append("")
    return lines


# ---------- 8. Historique et sortie ----------

def previous_run(today):
    """Dernier fichier daté avant aujourd'hui, pour repérer les nouveautés."""
    dated = sorted(p for p in OUTPUT.glob("prospects-20*.csv") if p.stem[len("prospects-"):] < today.isoformat())
    return dated[-1] if dated else None


def read_rows(path):
    if not path or not Path(path).exists():
        return [], []
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return list(reader), reader.fieldnames or []


def apply_history(rows, today):
    prev_path = previous_run(today)
    prev_rows, _ = read_rows(prev_path)
    prev = {r["domaine"]: r for r in prev_rows}
    latest_rows, latest_fields = read_rows(LATEST)
    latest = {r["domaine"]: r for r in latest_rows}
    user_fields = [c for c in latest_fields if c not in PIPELINE_FIELDS]

    for r in rows:
        before = prev.get(r["domaine"]) or latest.get(r["domaine"])
        r["vu_depuis"] = (before or {}).get("vu_depuis") or today.isoformat()
        old = prev.get(r["domaine"])
        if r["tier"] not in TIER_ORDER:
            r["nouveau"] = ""
        elif prev and old is None:
            r["nouveau"] = "Nouveau"
        elif old and old.get("tier") in TIER_ORDER and TIER_ORDER[r["tier"]] < TIER_ORDER[old["tier"]]:
            r["nouveau"] = f"Monte {old['tier']} → {r['tier']}"
        else:
            r["nouveau"] = ""
        for field in user_fields:
            r[field] = latest.get(r["domaine"], {}).get(field, "")
    return user_fields, prev_path


def write_csv(path, rows, fields):
    for attempt in range(3):
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fields, restval="", extrasaction="ignore")
                writer.writeheader()
                writer.writerows(rows)
            return
        except PermissionError:
            if attempt == 2:
                sys.exit(f"ERREUR : impossible d'écrire {path}. Ferme le fichier dans Excel puis relance.")
            print(f"  {path.name} est ouvert dans un autre programme (Excel ?), nouvel essai dans 5 s…", flush=True)
            time.sleep(5)


def write_summary(path, rows, sources, warnings, prev_file, elapsed, tracker_size, today=None, changes=None):
    """changes : événements de ce run (db.run_events), pour la section « Ce qui a changé » ; None = pas d'historique."""
    today = today or date.today()
    tiers = {t: sum(r["tier"] == t for r in rows) for t in TIER_ORDER}
    excluded = sum(r["tier"].startswith("Exclu") for r in rows)
    new = [r for r in rows if r.get("nouveau")]
    site_errors = sum(bool(r.get("erreur_site")) for r in rows)
    lines = [
        f"# Prospection : run du {today.isoformat()}",
        "",
        f"- Entreprises analysées : **{len(rows)}** (sources : {', '.join(n for n, _ in sources) or 'aucune'})",
        f"- Tier A : **{tiers['A']}** · Tier B : **{tiers['B']}** · Tier C : {tiers['C']} · Exclues : {excluded}",
        f"- Exclusions candidatures : {tracker_size} entreprises dans le tracker",
        f"- Nouveautés depuis {prev_file.stem.replace('prospects-', '') if prev_file else '(premier run)'} : **{len(new)}**",
    ]
    if changes is not None:
        lines.append(f"- Événements détectés : **{len(changes)}**")
    lines += [
        f"- Sites injoignables : {site_errors} (réessayés au prochain run)",
        f"- Durée : {elapsed:.0f} s",
        "",
    ]
    if warnings:
        lines += ["## Avertissements", ""] + [f"- {w}" for w in warnings] + [""]
    if changes is not None:
        lines += changes_section(changes)
    top = [r for r in rows if r["tier"] in {"A", "B"}][:20]
    if top:
        lines += ["## Top 20", "", "| Tier | Score | Nouveau | Entreprise | Domaine | Angle |", "|---|---|---|---|---|---|"]
        lines += [
            f"| {r['tier']} | {r['score']} | {r.get('nouveau', '')} | {r['entreprise']} | {r['domaine']} | {r.get('angle_suggere', '')} |"
            for r in top
        ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tiers, len(new)


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Pipeline complet de prospection (sources → score → CSV).",
                                     formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    parser.add_argument("--inputs", default=str(INPUTS), help="dossier des exports CSV à inclure (défaut : data/inputs)")
    parser.add_argument("--no-betakit", action="store_true", help="ne pas rafraîchir ni inclure BetaKit")
    parser.add_argument("--no-yc", action="store_true", help="ne pas rafraîchir ni inclure YC")
    parser.add_argument("--no-linkedin", action="store_true", help="sauter la recherche des pages LinkedIn")
    parser.add_argument("--tracker", default=None, help="job_search_tracker.csv (défaut : config.json, sinon ../ai-job-search)")
    parser.add_argument("--refresh", action="store_true", help="relit tous les sites et toutes les offres, même s'ils ne sont pas dus")
    parser.add_argument("--refresh-sources", action="store_true", help="re-télécharge BetaKit et YC même si déjà faits aujourd'hui")
    parser.add_argument("--limit", type=int, default=0, help="n'analyse que les N premières entreprises (test rapide)")
    parser.add_argument("--db", default=str(db.DB_PATH), help="base historique SQLite (défaut : data/history.db)")
    parser.add_argument("--workers", type=int, default=24)
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args(argv)

    started, today, warnings = time.time(), date.today(), []
    try:
        grid = scoring_config()  # une faute de frappe dans config.json arrête le run tout de suite, pas après la collecte
    except ValueError as exc:
        sys.exit(f"ERREUR : {exc}")
    OUTPUT.mkdir(parents=True, exist_ok=True)

    conn = db.connect(args.db)
    imported = db.import_legacy_caches(conn, CACHE)  # une seule fois : ne fait rien si déjà importé
    if not imported.get("already_imported"):
        print(f"Historique : {imported['scans']} lectures de sites importées des anciens caches", flush=True)
    run_id = db.start_run(conn, vars(args))
    stats = {"companies": 0, "sites_read": 0, "errors": 0}
    try:
        run(args, conn, run_id, grid, today, started, warnings, stats)
    except BaseException as exc:  # y compris sys.exit : le run est clos proprement, puis l'erreur remonte
        warnings.append(f"Run interrompu : {type(exc).__name__}: {exc}"[:300])
        raise
    finally:
        db.finish_run(conn, run_id, warnings=warnings, **stats)
        conn.close()


def run(args, conn, run_id, grid, today, started, warnings, stats):
    """Corps du pipeline. Chaque écriture en base est validée tout de suite : un plantage garde ce qui est fait."""
    sources = collect_sources(args, warnings)
    records = merge_sources(sources, warnings)
    if not records:
        sys.exit("Aucune entreprise à analyser : vérifie les sources et data/inputs/.")
    if args.limit:
        records = records[: args.limit]
        warnings.append(f"Run partiel : --limit {args.limit}")
    stats["companies"] = len(records)

    print("\n== Score ==", flush=True)
    tracker = Path(args.tracker) if args.tracker else default_tracker()
    applied = load_applied(tracker, required=bool(args.tracker))
    if not applied:
        warnings.append(f"Tracker de candidatures absent ou vide ({tracker}) : aucune exclusion CDI")
    _, excluded = exclusions(records, applied, grid)
    record_companies(conn, records, excluded, run_id, today)

    domains = [r["domain"] for r in records]
    stores = {"detection": HistoryStore(conn, "scan", run_id, today, domains, args.refresh),
              "ats": HistoryStore(conn, "jobs", run_id, today, domains, args.refresh)}
    try:
        rows, _ = score_records(records, applied, CACHE, args.workers, args.timeout, refresh=args.refresh, today=today,
                                stores=stores)
    finally:
        scans = stores["detection"].done
        stats.update(sites_read=len(scans), errors=sum(is_error(d) for d in scans.values()))
    for r in rows:
        if r["tier"] in TIER_ORDER:
            db.record_score(conn, r["domaine"], run_id, today, r)

    sources_by_domain = {r["domain"]: r["sources"] for r in records}
    for r in rows:
        r["sources"] = sources_by_domain.get(r["domaine"], "")
        r["signaux_recents"] = recent_signals(conn, r["domaine"], today)
    changes = db.run_events(conn, run_id, since_day=today - timedelta(days=SIGNALS_DAYS))

    if not args.no_linkedin:
        add_linkedin(rows, args.workers, args.timeout, args.refresh)

    user_fields, prev_file = apply_history(rows, today)
    fields = PIPELINE_FIELDS + user_fields
    dated = OUTPUT / f"prospects-{today.isoformat()}.csv"
    write_csv(dated, rows, fields)
    if args.limit:
        print(f"\n(Run partiel : {LATEST.name} n'est pas mis à jour.)")
    else:
        try:
            shutil.copyfile(dated, LATEST)
        except PermissionError:
            warnings.append(f"{LATEST.name} ouvert dans Excel : non mis à jour, le fichier daté est à jour")
    summary = OUTPUT / f"resume-{today.isoformat()}.md"
    tiers, new = write_summary(summary, rows, sources, warnings, prev_file, time.time() - started, len(applied),
                               today=today, changes=changes)

    print(f"\n== Résultat ==\nTier A : {tiers['A']} · Tier B : {tiers['B']} · Tier C : {tiers['C']} · Nouveautés : {new}"
          f" · Événements : {len(changes)}")
    for w in warnings:
        print(f"  ! {w}")
    print(f"→ {dated}\n→ {summary}")


if __name__ == "__main__":
    main()
