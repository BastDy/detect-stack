"""Partie « Timing » du score, tirée des événements de data/history.db (voir SCHEMA.md).

score_prospects.score() calcule le Profil et le Timing visible sans historique (offres ouvertes, levée).
Ce module y ajoute les événements récents (nouveau leader recruté, CRM installé, offre RevOps non pourvue…),
chacun avec une demi-vie : un signal perd la moitié de ses points tous les `decay[type]` jours.

Règles :
  - un type d'événement compte une seule fois par entreprise (le plus récent, donc le plus fort) ;
  - les événements de plus de signals.event_days jours sont ignorés ;
  - `funding` n'est pas lu ici : la levée est déjà comptée (avec sa demi-vie) par score() ;
  - `tool_added` ne compte que pour un CRM ou un outil marketing de CRM (HubSpot, Salesforce, Pipedrive…).
"""
from datetime import date, timedelta

from score_prospects import COMPETITOR_CRM, TIER_ORDER, decayed, tier_of

CRM_TOOLS = {"HubSpot"} | COMPETITOR_CRM

# type d'événement -> (clé de points / demi-vie dans la grille, libellé du détail, angle d'approche)
EVENT_SIGNALS = {
    "leadership_hire": ("leadership_hire", "Recrute à la direction : {detail}",
                        "Recrute « {detail} » : le nouveau leader voudra revoir process et outils, arriver avant lui"),
    "tool_added": ("crm_installed", "{detail} installé",
                   "{detail} installé récemment : implémentation, migration des données et adoption"),
    "second_portal": ("second_portal", "Plusieurs portals HubSpot",
                      "Plusieurs portals HubSpot ({detail}) : consolidation"),
    "hiring_spike": ("hiring_spike", "Hausse des recrutements sales ({detail})",
                     "Recrutements sales en hausse ({detail}) : structurer process et reporting avant l'arrivée des recrues"),
    "posting_stale": ("posting_stale", "Offre RevOps non pourvue : {detail}",
                      "« {detail} » non pourvu depuis 60+ jours : RevOps en intérim"),
    "posting_crm_mismatch": ("posting_crm_mismatch", "Offre : autre CRM que le site",
                             "Offre qui ne demande pas le CRM du site ({detail}) : migration ou double CRM probable"),
    "posting_first_hire": ("posting_first_hire", "Premier recrutement : {detail}",
                           "Premier recrutement ({detail}) : poser CRM et process avant l'arrivée"),
    "posting_build": ("posting_build", "Offre : process à construire",
                      "L'offre demande de construire process et reporting ({detail}) : renfort RevOps"),
}


def event_signals(conn, domain, today, grid):
    """Signaux Timing tirés des événements -> [{"type", "day", "age", "points", "label", "angle"}], du plus fort au plus faible."""
    since = today - timedelta(days=grid["signals"]["event_days"])
    rows = conn.execute(
        f"""SELECT day, type, detail FROM events WHERE domain = ? AND day >= ? AND day <= ?
            AND type IN ({', '.join('?' * len(EVENT_SIGNALS))}) ORDER BY day DESC, id DESC""",
        (domain, since.isoformat(), today.isoformat(), *EVENT_SIGNALS)).fetchall()
    best = {}
    for row in rows:
        kind, detail = row["type"], row["detail"] or ""
        if kind == "tool_added" and detail not in CRM_TOOLS:
            continue
        key, label, angle = EVENT_SIGNALS[kind]
        age = (today - date.fromisoformat(row["day"][:10])).days
        points = decayed(grid["points"][key], age, grid["decay"][key])
        if points <= 0 or (key in best and best[key]["points"] >= points):
            continue
        best[key] = {"type": kind, "day": row["day"][:10], "age": age, "points": points,
                     "label": f"{label.format(detail=detail)}, il y a {age} j",
                     "angle": angle.format(detail=detail)}
    return sorted(best.values(), key=lambda s: (-s["points"], s["age"]))


def apply_timing(conn, rows, today, grid, max_angles=2):
    """Ajoute les événements au score des lignes scorées (A/B/C), recalcule le tier et retrie les lignes en place.

    Les angles tirés des événements passent devant ceux de score() : ce qui vient de se passer est le meilleur motif
    de contact. Renvoie le nombre d'entreprises dont le Timing a changé grâce à l'historique.
    """
    touched = 0
    for r in rows:
        if r["tier"] not in TIER_ORDER:
            continue
        signals = event_signals(conn, r["domaine"], today, grid)
        if not signals:
            continue
        touched += 1
        extra = sum(s["points"] for s in signals)
        r["score_timing"] = (r.get("score_timing") or 0) + extra
        r["score"] = (r.get("score") or 0) + extra
        r["tier"] = tier_of(r["score"], r["score_timing"], grid)
        r["detail_score"] = "; ".join(filter(None, [r.get("detail_score", "")]
                                             + [f"{s['label']} +{s['points']}" for s in signals]))
        angles = [s["angle"] for s in signals] + [a for a in (r.get("angle_suggere") or "").split(" | ") if a]
        r["angle_suggere"] = " | ".join(list(dict.fromkeys(angles))[:max_angles])
    rows.sort(key=lambda r: (TIER_ORDER.get(r["tier"], 3), -(r["score"] or 0), -(r.get("score_timing") or 0),
                             r["entreprise"]))
    return touched
