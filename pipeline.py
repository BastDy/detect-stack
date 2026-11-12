"""Pipeline complet de prospection, en une commande.

    python pipeline.py

Étapes :
  1. Sources     BetaKit (levées 12 mois) + YC Canada + tous les CSV posés dans data/inputs/ (exports Apollo…)
  2. Fusion      dédoublonnage par domaine, les infos des différentes sources se complètent
  3. Exclusions  entreprises où tu as déjà postulé (tracker ai-job-search), moins de 10 salariés
  4. Détection   stack du site (HubSpot, CRM concurrents, outils) : cache 30 jours
  5. Offres      postes ouverts via Greenhouse / Lever / Ashby / Workable : cache 3 jours
  6. Score       grille ICP -> tier A / B / C + angle d'approche
  7. LinkedIn    page entreprise pour les tiers A et B : cache 90 jours
  8. Sortie      data/output/prospects-AAAA-MM-JJ.csv + prospects-latest.csv + resume-AAAA-MM-JJ.md

Les colonnes que tu ajoutes à la main dans prospects-latest.csv (Notes, Statut…) sont conservées d'un run à l'autre.
"""
import argparse
import csv
import shutil
import sys
import time
from datetime import date
from pathlib import Path

import betakit_funding
import find_linkedin
import yc_companies
from common import CACHE, INPUTS, OUTPUT, Cache, run_stage
from score_prospects import (
    OUTPUT_FIELDS, TIER_ORDER, default_tracker, load_applied, parse_date, read_export, score_records,
    scoring_config,
)

LINKEDIN_MAX_AGE_DAYS = 90
PIPELINE_FIELDS = (
    OUTPUT_FIELDS[:2] + ["nouveau"] + OUTPUT_FIELDS[2:] + ["sources", "vu_depuis"]
)
LATEST = OUTPUT / "prospects-latest.csv"


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

def merge_sources(sources, warnings):
    merged = {}
    print("\n== Fusion ==", flush=True)
    for name, path in sources:
        try:
            records, without_site, _ = read_export(path)
        except (ValueError, OSError) as exc:
            warnings.append(f"{name} ignoré : {exc}")
            print("  " + warnings[-1], flush=True)
            continue
        print(f"  {name:<28} {len(records):>4} entreprises · {without_site} sans site", flush=True)
        for rec in records:
            current = merged.get(rec["domain"])
            if current is None:
                merged[rec["domain"]] = {**rec, "sources": [name]}
                continue
            current["sources"].append(name)
            for key, value in rec.items():
                if key == "last_raised":
                    if (parse_date(value) or date.min) > (parse_date(current[key]) or date.min):
                        current[key] = value
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


def write_summary(path, rows, sources, warnings, prev_file, elapsed, tracker_size):
    tiers = {t: sum(r["tier"] == t for r in rows) for t in TIER_ORDER}
    excluded = sum(r["tier"].startswith("Exclu") for r in rows)
    new = [r for r in rows if r.get("nouveau")]
    site_errors = sum(bool(r.get("erreur_site")) for r in rows)
    lines = [
        f"# Prospection : run du {date.today().isoformat()}",
        "",
        f"- Entreprises analysées : **{len(rows)}** (sources : {', '.join(n for n, _ in sources) or 'aucune'})",
        f"- Tier A : **{tiers['A']}** · Tier B : **{tiers['B']}** · Tier C : {tiers['C']} · Exclues : {excluded}",
        f"- Exclusions candidatures : {tracker_size} entreprises dans le tracker",
        f"- Nouveautés depuis {prev_file.stem.replace('prospects-', '') if prev_file else '(premier run)'} : **{len(new)}**",
        f"- Sites injoignables : {site_errors} (réessayés au prochain run)",
        f"- Durée : {elapsed:.0f} s",
        "",
    ]
    if warnings:
        lines += ["## Avertissements", ""] + [f"- {w}" for w in warnings] + [""]
    top = [r for r in rows if r["tier"] in {"A", "B"}][:20]
    if top:
        lines += ["## Top 20", "", "| Tier | Score | Nouveau | Entreprise | Domaine | Angle |", "|---|---|---|---|---|---|"]
        lines += [
            f"| {r['tier']} | {r['score']} | {r.get('nouveau', '')} | {r['entreprise']} | {r['domaine']} | {r.get('angle_suggere', '')} |"
            for r in top
        ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tiers, len(new)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Pipeline complet de prospection (sources → score → CSV).",
                                     formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    parser.add_argument("--inputs", default=str(INPUTS), help="dossier des exports CSV à inclure (défaut : data/inputs)")
    parser.add_argument("--no-betakit", action="store_true", help="ne pas rafraîchir ni inclure BetaKit")
    parser.add_argument("--no-yc", action="store_true", help="ne pas rafraîchir ni inclure YC")
    parser.add_argument("--no-linkedin", action="store_true", help="sauter la recherche des pages LinkedIn")
    parser.add_argument("--tracker", default=None, help="job_search_tracker.csv (défaut : config.json, sinon ../ai-job-search)")
    parser.add_argument("--refresh", action="store_true", help="ignore tous les caches de sites et d'offres")
    parser.add_argument("--refresh-sources", action="store_true", help="re-télécharge BetaKit et YC même si déjà faits aujourd'hui")
    parser.add_argument("--limit", type=int, default=0, help="n'analyse que les N premières entreprises (test rapide)")
    parser.add_argument("--workers", type=int, default=24)
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args()

    started, today, warnings = time.time(), date.today(), []
    try:
        scoring_config()  # une faute de frappe dans config.json arrête le run tout de suite, pas après la collecte
    except ValueError as exc:
        sys.exit(f"ERREUR : {exc}")
    OUTPUT.mkdir(parents=True, exist_ok=True)

    sources = collect_sources(args, warnings)
    records = merge_sources(sources, warnings)
    if not records:
        sys.exit("Aucune entreprise à analyser : vérifie les sources et data/inputs/.")
    if args.limit:
        records = records[: args.limit]
        warnings.append(f"Run partiel : --limit {args.limit}")

    print("\n== Score ==", flush=True)
    tracker = Path(args.tracker) if args.tracker else default_tracker()
    applied = load_applied(tracker, required=bool(args.tracker))
    if not applied:
        warnings.append(f"Tracker de candidatures absent ou vide ({tracker}) : aucune exclusion CDI")
    rows, _ = score_records(records, applied, CACHE, args.workers, args.timeout, refresh=args.refresh, today=today)
    sources_by_domain = {r["domain"]: r["sources"] for r in records}
    for r in rows:
        r["sources"] = sources_by_domain.get(r["domaine"], "")

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
    tiers, new = write_summary(summary, rows, sources, warnings, prev_file, time.time() - started, len(applied))

    print(f"\n== Résultat ==\nTier A : {tiers['A']} · Tier B : {tiers['B']} · Tier C : {tiers['C']} · Nouveautés : {new}")
    for w in warnings:
        print(f"  ! {w}")
    print(f"→ {dated}\n→ {summary}")


if __name__ == "__main__":
    main()
