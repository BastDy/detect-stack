"""Historique SQLite des observations (sites, offres, scores) et événements déduits des changements.

    conn = db.connect()                      # crée ou met à jour data/history.db
    run_id = db.start_run(conn, {"limit": 25})
    db.record_scan(conn, "acme.com", detection, run_id, "2026-11-11T09:00:00")
    db.finish_run(conn, run_id, companies=300, sites_read=120)

Règle d'or (anti-fausse alerte) : une lecture en échec ne change rien. Un outil ou une offre n'est déclaré
disparu qu'après DEFAULT_SIGNALS["absent_reads"] lectures réussies consécutives (au plus une par jour) où il manque.
Le premier relevé d'un domaine remplit l'état sans émettre d'événement de changement.
Voir SCHEMA.md pour le détail des tables et des requêtes types.
"""
import json
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path

from common import DATA, is_error, job_category

DB_PATH = DATA / "history.db"
SCHEMA_VERSION = 1

# Seuils des événements.
DEFAULT_SIGNALS = {
    "absent_reads": 2,   # lectures réussies consécutives sans l'outil / l'offre avant de le déclarer disparu
    "stale_days": 60,    # âge d'une offre RevOps encore ouverte pour émettre posting_stale
    "spike_min": 3,      # hausse minimale du nombre d'offres sales ouvertes pour hiring_spike
}
# Délais de relecture, en jours. "hot" = dernier tier A ou B, "cold" = C, exclu ou inconnu.
DEFAULT_REFRESH = {
    "scan": {"hot": 1, "cold": 30, "error": 1},
    "jobs": {"hot": 1, "cold": 7, "error": 1},
}
TIERS = {"A": 0, "B": 1, "C": 2}
PORTAL_PREFIX = "HubSpot portal "

SCHEMA = """
CREATE TABLE runs (
    id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, options TEXT,
    companies INTEGER, sites_read INTEGER, errors INTEGER, warnings TEXT
);
CREATE TABLE companies (
    domain TEXT PRIMARY KEY, name TEXT, employees INTEGER, industry TEXT, city TEXT, province TEXT, country TEXT,
    linkedin_url TEXT, sources TEXT, excluded_reason TEXT, first_seen TEXT, last_seen TEXT
);
CREATE TABLE funding_rounds (
    domain TEXT NOT NULL, announced_on TEXT NOT NULL, amount REAL, currency TEXT, round TEXT NOT NULL DEFAULT '',
    article_url TEXT, PRIMARY KEY (domain, announced_on, round)
);
CREATE TABLE site_scans (
    id INTEGER PRIMARY KEY, domain TEXT NOT NULL, run_id INTEGER, scanned_at TEXT NOT NULL, ok INTEGER NOT NULL,
    hubspot TEXT, portal_ids TEXT, region TEXT, hubspot_signals TEXT, tools TEXT, saas_hints TEXT, ats_links TEXT,
    pages_read INTEGER, error TEXT
);
CREATE TABLE stack (
    domain TEXT NOT NULL, tool TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'present', gone_since TEXT, absent_count INTEGER NOT NULL DEFAULT 0,
    checked_on TEXT, PRIMARY KEY (domain, tool)
);
CREATE TABLE job_reads (
    id INTEGER PRIMARY KEY, domain TEXT NOT NULL, ats TEXT NOT NULL, run_id INTEGER, day TEXT NOT NULL,
    ok INTEGER NOT NULL, postings INTEGER, sales_open INTEGER
);
CREATE TABLE job_postings (
    domain TEXT NOT NULL, ats TEXT NOT NULL, posting_id TEXT NOT NULL, title TEXT, category TEXT, location TEXT,
    url TEXT, published_at TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, closed_on TEXT,
    absent_count INTEGER NOT NULL DEFAULT 0, checked_on TEXT, PRIMARY KEY (domain, ats, posting_id)
);
CREATE TABLE scores (
    domain TEXT NOT NULL, run_id INTEGER NOT NULL, day TEXT NOT NULL, tier TEXT, score INTEGER, detail TEXT,
    angle TEXT, PRIMARY KEY (domain, run_id)
);
CREATE TABLE events (
    id INTEGER PRIMARY KEY, day TEXT NOT NULL, domain TEXT NOT NULL, type TEXT NOT NULL, detail TEXT,
    run_id INTEGER, ref TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_events_day ON events(day);
CREATE INDEX idx_events_domain ON events(domain);
CREATE UNIQUE INDEX idx_events_unique ON events(day, domain, type, ref);
CREATE INDEX idx_scans_domain ON site_scans(domain, scanned_at);
CREATE INDEX idx_job_reads_domain ON job_reads(domain, day);
CREATE INDEX idx_scores_domain ON scores(domain, day);

CREATE VIEW v_latest_scores AS
SELECT s.domain, c.name, s.day, s.tier, s.score, s.detail, s.angle, s.run_id
FROM scores s LEFT JOIN companies c USING (domain)
WHERE s.rowid = (SELECT s2.rowid FROM scores s2 WHERE s2.domain = s.domain ORDER BY s2.day DESC, s2.run_id DESC LIMIT 1);

CREATE VIEW v_current_stack AS
SELECT st.domain, c.name, st.tool, st.first_seen, st.last_seen, st.absent_count
FROM stack st LEFT JOIN companies c USING (domain)
WHERE st.status = 'present';

CREATE VIEW v_open_postings AS
SELECT p.domain, c.name, p.ats, p.posting_id, p.title, p.category, p.location, p.url, p.published_at,
       p.first_seen, p.last_seen, p.absent_count,
       CAST(julianday(date('now', 'localtime')) - julianday(substr(COALESCE(p.published_at, p.first_seen), 1, 10))
            AS INTEGER) AS age_days
FROM job_postings p LEFT JOIN companies c USING (domain)
WHERE p.closed_on IS NULL;

CREATE VIEW v_recent_events AS
SELECT e.day, e.domain, c.name, e.type, e.detail, e.run_id
FROM events e LEFT JOIN companies c USING (domain)
WHERE e.day >= date('now', 'localtime', '-30 days')
ORDER BY e.day DESC, e.id DESC;
"""
# Migrations futures : {version cible: script SQL}, appliquées dans l'ordre par connect().
MIGRATIONS = {}


# ---------- Connexion ----------

def connect(path=DB_PATH):
    """Ouvre la base (':memory:' accepté), la crée ou l'amène à SCHEMA_VERSION. Lignes renvoyées en sqlite3.Row."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        conn.close()
        raise RuntimeError(f"{path} : schéma v{version} plus récent que ce code (v{SCHEMA_VERSION})")
    if version == 0:
        conn.executescript(SCHEMA)
        version = 1
    for target in range(version + 1, SCHEMA_VERSION + 1):
        conn.executescript(MIGRATIONS[target])
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    return conn


# ---------- Petits utilitaires ----------

def _now():
    return datetime.now().isoformat(timespec="seconds")


def _day(value):
    """date, datetime ou texte ISO -> 'YYYY-MM-DD'."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()[:10]
    return str(value)[:10]


def _as_date(value):
    """Texte ISO, date ou timestamp en millisecondes (Lever) -> date ; None si illisible."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) or str(value).isdigit():
        return datetime.fromtimestamp(int(value) / 1000).date()
    try:
        return date.fromisoformat(_day(value))
    except ValueError:
        return None


def _split(value, sep):
    return [part.strip() for part in (value or "").split(sep) if part.strip()]


def _json(values):
    return json.dumps(list(values), ensure_ascii=False)


def _event(conn, day, domain, kind, detail, run_id, ref=""):
    """Ajoute un événement ; ignoré s'il existe déjà le même jour (idempotence des rejeux)."""
    conn.execute("INSERT OR IGNORE INTO events (day, domain, type, detail, run_id, ref) VALUES (?, ?, ?, ?, ?, ?)",
                 (day, domain, kind, detail, run_id, ref))


def _event_ever(conn, domain, kind, ref):
    return conn.execute("SELECT 1 FROM events WHERE domain = ? AND type = ? AND ref = ?", (domain, kind, ref)).fetchone()


# Catégorie d'un intitulé : 'revops', 'leadership', 'sales' ou 'other'. Source unique : common.job_category.
categorize = job_category


def tools_of(detection):
    """Outils déduits d'un résultat de detect_stack.analyze : HubSpot, autres outils, un 'HubSpot portal <id>' par portal."""
    tools = set(_split(detection.get("autres_outils"), ","))
    if detection.get("hubspot") in {"Oui", "Probable"}:
        tools.add("HubSpot")
    tools.update(PORTAL_PREFIX + pid for pid in _split(detection.get("portal_id"), ";"))
    return tools


# ---------- Runs et entreprises ----------

def start_run(conn, options):
    with conn:
        cur = conn.execute("INSERT INTO runs (started_at, options) VALUES (?, ?)",
                           (_now(), json.dumps(options or {}, ensure_ascii=False, default=str)))
    return cur.lastrowid


RUN_STATS = {"finished_at", "companies", "sites_read", "errors", "warnings"}


def finish_run(conn, run_id, **stats):
    """Clôt un run. stats : companies, sites_read, errors, warnings (liste), finished_at (défaut : maintenant)."""
    unknown = set(stats) - RUN_STATS
    if unknown:
        raise ValueError(f"finish_run : statistiques inconnues {sorted(unknown)}")
    stats.setdefault("finished_at", _now())
    if "warnings" in stats:
        stats["warnings"] = _json(stats["warnings"] or [])
    with conn:
        conn.execute(f"UPDATE runs SET {', '.join(f'{k} = ?' for k in stats)} WHERE id = ?", (*stats.values(), run_id))


def _parse_int(value):
    match = re.search(r"\d[\d,]*", str(value or ""))
    return int(match.group().replace(",", "")) if match else None


def upsert_company(conn, rec, day):
    """Crée ou complète une entreprise depuis un record read_export / merge_sources. Une valeur vide n'écrase rien.

    sources ("a; b" ou liste) est fusionnée avec les sources déjà connues. excluded_reason n'est modifié que si
    la clé est présente dans rec (une chaîne vide efface alors l'exclusion).
    """
    day = _day(day)
    sources = rec.get("sources") or []
    sources = _split(sources, ";") if isinstance(sources, str) else list(sources)
    values = {
        "name": rec.get("company"), "employees": _parse_int(rec.get("employees")), "industry": rec.get("industry"),
        "city": rec.get("city"), "province": rec.get("state"), "country": rec.get("country"),
        "linkedin_url": rec.get("linkedin"),
    }
    values = {k: (v or None) for k, v in values.items()}
    with conn:
        old = conn.execute("SELECT sources, excluded_reason FROM companies WHERE domain = ?", (rec["domain"],)).fetchone()
        known = json.loads(old["sources"]) if old and old["sources"] else []
        merged = _json(dict.fromkeys(known + sources))
        excluded = (rec.get("excluded_reason") or None) if "excluded_reason" in rec else (old["excluded_reason"] if old else None)
        conn.execute(
            f"""INSERT INTO companies (domain, {', '.join(values)}, sources, excluded_reason, first_seen, last_seen)
                VALUES (?, {', '.join('?' * len(values))}, ?, ?, ?, ?)
                ON CONFLICT(domain) DO UPDATE SET
                {', '.join(f'{k} = COALESCE(excluded.{k}, {k})' for k in values)},
                sources = excluded.sources, excluded_reason = excluded.excluded_reason, last_seen = excluded.last_seen""",
            (rec["domain"], *values.values(), merged, excluded, day, day))


def record_funding(conn, domain, announced_on, amount, currency, round, url, run_id):
    """Enregistre une levée ; l'événement funding n'est émis que la première fois qu'on la voit. Renvoie True si nouvelle."""
    announced_on, round = _day(announced_on), round or ""
    with conn:
        cur = conn.execute("INSERT OR IGNORE INTO funding_rounds VALUES (?, ?, ?, ?, ?, ?)",
                           (domain, announced_on, amount, currency, round, url))
        if cur.rowcount:
            detail = " ".join(str(x) for x in (round, amount, currency) if x not in (None, "")) or "levée"
            _event(conn, announced_on, domain, "funding", detail, run_id, ref=f"{announced_on}:{round}")
    return bool(cur.rowcount)


# ---------- Stack des sites ----------

def record_scan(conn, domain, detection, run_id, scanned_at, signals=None):
    """Enregistre un résultat de detect_stack.analyze. Si la lecture a réussi, met à jour stack et émet les événements."""
    with conn:
        _record_scan(conn, domain, detection, run_id, scanned_at, signals or DEFAULT_SIGNALS, emit=True)


def _record_scan(conn, domain, det, run_id, scanned_at, signals, emit):
    ok = not is_error(det)
    first = not conn.execute("SELECT 1 FROM site_scans WHERE domain = ? AND ok = 1", (domain,)).fetchone()
    conn.execute(
        """INSERT INTO site_scans (domain, run_id, scanned_at, ok, hubspot, portal_ids, region, hubspot_signals, tools,
                                   saas_hints, ats_links, pages_read, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (domain, run_id, scanned_at, int(ok), det.get("hubspot"), _json(_split(det.get("portal_id"), ";")),
         det.get("region") or None, _json(_split(det.get("signaux_hubspot"), ",")),
         _json(_split(det.get("autres_outils"), ",")), _json(_split(det.get("indices_saas"), ",")),
         _json(_split(det.get("ats"), ";")), det.get("pages_analysees"), det.get("erreur") or None))
    if ok:
        _update_stack(conn, domain, tools_of(det), _day(scanned_at), run_id, signals, emit and not first, emit)


def _update_stack(conn, domain, tools, day, run_id, signals, emit_changes, emit_state):
    rows = {r["tool"]: r for r in conn.execute("SELECT * FROM stack WHERE domain = ?", (domain,))}

    def portals():
        return sorted(r["tool"][len(PORTAL_PREFIX):] for r in conn.execute(
            "SELECT tool FROM stack WHERE domain = ? AND status = 'present' AND tool LIKE ?", (domain, PORTAL_PREFIX + "%")))

    before = portals()
    for tool in sorted(tools):
        row = rows.get(tool)
        if row is None:
            conn.execute("INSERT INTO stack (domain, tool, first_seen, last_seen, checked_on) VALUES (?, ?, ?, ?, ?)",
                         (domain, tool, day, day, day))
        else:
            conn.execute("""UPDATE stack SET last_seen = ?, checked_on = ?, absent_count = 0, status = 'present',
                            gone_since = NULL WHERE domain = ? AND tool = ?""", (day, day, domain, tool))
        if emit_changes and (row is None or row["status"] == "gone"):
            _event(conn, day, domain, "tool_added", tool, run_id, ref=tool)
    for tool, row in rows.items():
        # Une seule lecture comptée par jour : rejouer un scan le même jour ne fait pas avancer absent_count.
        if tool in tools or row["status"] == "gone" or row["checked_on"] == day:
            continue
        absent = row["absent_count"] + 1
        if absent >= signals["absent_reads"]:
            conn.execute("""UPDATE stack SET absent_count = ?, checked_on = ?, status = 'gone', gone_since = ?
                            WHERE domain = ? AND tool = ?""", (absent, day, day, domain, tool))
            if emit_state:
                _event(conn, day, domain, "tool_removed", tool, run_id, ref=tool)
        else:
            conn.execute("UPDATE stack SET absent_count = ?, checked_on = ? WHERE domain = ? AND tool = ?",
                         (absent, day, domain, tool))
    after = portals()
    if emit_state and len(after) >= 2 and len(before) < 2:
        _event(conn, day, domain, "second_portal", "portals HubSpot : " + ", ".join(after), run_id, ref=",".join(after))


# ---------- Offres d'emploi ----------

def record_postings(conn, domain, ats, postings, run_id, day, signals=None):
    """Enregistre un relevé d'offres d'un board ATS (ex. 'greenhouse:acme').

    postings : liste de {posting_id, title, category, location, url, published_at} ; None = lecture en échec,
    seule la tentative est notée (job_reads.ok = 0), rien n'est conclu sur les offres.
    """
    with conn:
        _record_postings(conn, domain, ats, postings, run_id, _day(day), signals or DEFAULT_SIGNALS, emit=True)


def _record_postings(conn, domain, ats, postings, run_id, day, signals, emit):
    if postings is None:
        conn.execute("INSERT INTO job_reads (domain, ats, run_id, day, ok) VALUES (?, ?, ?, ?, 0)", (domain, ats, run_id, day))
        return
    prev = conn.execute("""SELECT sales_open FROM job_reads WHERE domain = ? AND ats = ? AND ok = 1
                           ORDER BY day DESC, id DESC LIMIT 1""", (domain, ats)).fetchone()
    first = prev is None
    rows = {r["posting_id"]: r for r in conn.execute("SELECT * FROM job_postings WHERE domain = ? AND ats = ?", (domain, ats))}
    seen = {}
    for p in postings:
        pid = str(p.get("posting_id") or "").strip()
        if pid:
            seen.setdefault(pid, p)

    for pid, p in seen.items():
        row, ref = rows.get(pid), f"{ats}:{pid}"
        category = p.get("category") or categorize(p.get("title"))
        fields = (p.get("title"), category, p.get("location") or None, p.get("url") or None, p.get("published_at") or None)
        if row is None:
            conn.execute("""INSERT INTO job_postings (domain, ats, posting_id, title, category, location, url, published_at,
                            first_seen, last_seen, checked_on) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                         (domain, ats, pid, *fields, day, day, day))
        else:
            conn.execute("""UPDATE job_postings SET title = ?, category = ?, location = COALESCE(?, location),
                            url = COALESCE(?, url), published_at = COALESCE(?, published_at), last_seen = ?, checked_on = ?,
                            absent_count = 0, closed_on = NULL WHERE domain = ? AND ats = ? AND posting_id = ?""",
                         (*fields, day, day, domain, ats, pid))
        if not emit:
            continue
        if not first and (row is None or row["closed_on"]):
            _event(conn, day, domain, "posting_opened", p.get("title"), run_id, ref=ref)
        if category == "leadership" and not _event_ever(conn, domain, "leadership_hire", ref):
            _event(conn, day, domain, "leadership_hire", p.get("title"), run_id, ref=ref)

    for pid, row in rows.items():
        if pid in seen or row["closed_on"] or row["checked_on"] == day:
            continue
        absent = row["absent_count"] + 1
        closed = day if absent >= signals["absent_reads"] else None
        conn.execute("""UPDATE job_postings SET absent_count = ?, checked_on = ?, closed_on = ?
                        WHERE domain = ? AND ats = ? AND posting_id = ?""", (absent, day, closed, domain, ats, pid))
        if closed and emit:
            _event(conn, day, domain, "posting_closed", row["title"], run_id, ref=f"{ats}:{pid}")

    open_rows = conn.execute("SELECT * FROM job_postings WHERE domain = ? AND ats = ? AND closed_on IS NULL",
                             (domain, ats)).fetchall()
    if emit:
        today = date.fromisoformat(day)
        for row in open_rows:
            ref = f"{ats}:{row['posting_id']}"
            since = _as_date(row["published_at"]) or _as_date(row["first_seen"])
            age = (today - since).days if since else 0
            if row["category"] == "revops" and age > signals["stale_days"] and not _event_ever(conn, domain, "posting_stale", ref):
                _event(conn, day, domain, "posting_stale", f"{row['title']} (ouverte depuis {age} jours)", run_id, ref=ref)
    sales_open = sum(r["category"] == "sales" for r in open_rows)
    if emit and not first and sales_open - (prev["sales_open"] or 0) >= signals["spike_min"]:
        _event(conn, day, domain, "hiring_spike", f"offres sales ouvertes : {prev['sales_open'] or 0} → {sales_open}",
               run_id, ref=ats)
    conn.execute("INSERT INTO job_reads (domain, ats, run_id, day, ok, postings, sales_open) VALUES (?, ?, ?, ?, 1, ?, ?)",
                 (domain, ats, run_id, day, len(seen), sales_open))


def record_jobs_lookup(conn, domain, run_id, day, ok=True):
    """Note une recherche d'offres sans board : ok=True = aucun board trouvé, ok=False = la recherche a planté.

    Seule une ligne job_reads (ats = '') est écrite, pour que due_for_jobs sache quand reprendre ; aucune offre
    ni aucun événement. Ne pas utiliser record_postings dans ce cas : '' n'est pas un board.
    """
    with conn:
        conn.execute("INSERT INTO job_reads (domain, ats, run_id, day, ok, postings, sales_open) VALUES (?, '', ?, ?, ?, ?, ?)",
                     (domain, run_id, _day(day), int(ok), 0 if ok else None, 0 if ok else None))


# ---------- Scores ----------

def record_score(conn, domain, run_id, day, row):
    """Enregistre une ligne de score_prospects ; tier_up / tier_down si le tier A/B/C change vs le dernier tier A/B/C connu."""
    day, tier = _day(day), row.get("tier")
    score = row.get("score")
    with conn:
        prev = conn.execute("""SELECT tier FROM scores WHERE domain = ? AND run_id != ? AND tier IN ('A', 'B', 'C')
                               ORDER BY day DESC, run_id DESC LIMIT 1""", (domain, run_id)).fetchone()
        conn.execute("INSERT OR REPLACE INTO scores VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (domain, run_id, day, tier, score if score not in ("", None) else None,
                      row.get("detail_score") or None, row.get("angle_suggere") or None))
        if prev and tier in TIERS and tier != prev["tier"]:
            kind = "tier_up" if TIERS[tier] < TIERS[prev["tier"]] else "tier_down"
            _event(conn, day, domain, kind, f"{prev['tier']} → {tier}", run_id, ref=f"{prev['tier']}>{tier}")


# ---------- Lecture ----------

def _decode(row, json_fields):
    if row is None:
        return None
    out = dict(row)
    for key in json_fields:
        if out.get(key) is not None:
            out[key] = json.loads(out[key])
    return out


def last_scan(conn, domain):
    """Dernière lecture du site (réussie ou non), listes décodées ; None si jamais lu."""
    row = conn.execute("SELECT * FROM site_scans WHERE domain = ? ORDER BY scanned_at DESC, id DESC LIMIT 1",
                       (domain,)).fetchone()
    return _decode(row, ("portal_ids", "hubspot_signals", "tools", "saas_hints", "ats_links"))


def last_tier(conn, domain):
    """Dernier tier enregistré ('A', 'B', 'C', 'Exclu : …') ou None."""
    row = conn.execute("SELECT tier FROM scores WHERE domain = ? ORDER BY day DESC, run_id DESC LIMIT 1", (domain,)).fetchone()
    return row["tier"] if row else None


def _due(conn, domains, today, delays, latest_sql):
    today = date.fromisoformat(_day(today))
    due = []
    for domain in domains:
        last = conn.execute(latest_sql, (domain,)).fetchone()
        if last is None:
            due.append(domain)
            continue
        if not last["ok"]:
            wait = delays["error"]
        else:
            wait = delays["hot"] if last_tier(conn, domain) in {"A", "B"} else delays["cold"]
        if (today - date.fromisoformat(last["day"][:10])).days >= wait:
            due.append(domain)
    return due


def due_for_scan(conn, domains, today, policy=None):
    """Domaines dont le site est à relire : jamais lu, dernière lecture en erreur (1 j), tier A/B (1 j), sinon 30 j."""
    delays = (policy or DEFAULT_REFRESH)["scan"]
    return _due(conn, domains, today, delays,
                "SELECT scanned_at AS day, ok FROM site_scans WHERE domain = ? ORDER BY scanned_at DESC, id DESC LIMIT 1")


def due_for_jobs(conn, domains, today, policy=None):
    """Domaines dont les offres sont à relire : jamais lues, échec (1 j), tier A/B (1 j), sinon 7 j."""
    delays = (policy or DEFAULT_REFRESH)["jobs"]
    return _due(conn, domains, today, delays,
                "SELECT day, ok FROM job_reads WHERE domain = ? ORDER BY day DESC, id DESC LIMIT 1")


def recent_events(conn, domain, since_day, limit=3):
    rows = conn.execute("""SELECT day, type, detail, run_id FROM events WHERE domain = ? AND day >= ?
                           ORDER BY day DESC, id DESC LIMIT ?""", (domain, _day(since_day), limit))
    return [dict(r) for r in rows]


def run_events(conn, run_id, since_day=None):
    """Événements émis par un run (optionnellement datés de since_day ou après), avec le nom de l'entreprise."""
    rows = conn.execute("""SELECT e.day, e.domain, c.name, e.type, e.detail FROM events e LEFT JOIN companies c USING (domain)
                           WHERE e.run_id = ? AND e.day >= ? ORDER BY e.id""", (run_id, _day(since_day or "0000-00-00")))
    return [dict(r) for r in rows]


def last_job_read(conn, domain, ok_only=False):
    """Dernier relevé d'offres du domaine (ats '' = aucun board) ; None si jamais relevé."""
    row = conn.execute(f"""SELECT * FROM job_reads WHERE domain = ? {'AND ok = 1' if ok_only else ''}
                           ORDER BY day DESC, id DESC LIMIT 1""", (domain,)).fetchone()
    return dict(row) if row else None


def open_postings(conn, domain, ats):
    """Offres encore ouvertes d'un board, dans l'ordre où elles ont été vues pour la première fois."""
    rows = conn.execute("""SELECT * FROM job_postings WHERE domain = ? AND ats = ? AND closed_on IS NULL
                           ORDER BY rowid""", (domain, ats))
    return [dict(r) for r in rows]


# ---------- Import des anciens caches ----------

def _cache_lines(path):
    """Entrées {"domain", "fetched_at", "data"} lisibles d'un cache JSONL, par ordre chronologique."""
    entries = []
    if not Path(path).exists():
        return entries, 0
    skipped = 0
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            if entry.get("domain") and entry.get("fetched_at") and isinstance(entry.get("data"), dict):
                entries.append(entry)
                continue
        except (ValueError, AttributeError):
            pass
        skipped += 1
    entries.sort(key=lambda e: e["fetched_at"])
    return entries, skipped


def import_legacy_caches(conn, cache_dir):
    """Importe une seule fois detection.jsonl (stack des sites) comme premières observations, sans aucun événement.

    ats.jsonl n'est volontairement PAS importé : ses offres n'ont qu'un titre, il faudrait fabriquer des posting_id
    à partir du titre ; au premier vrai relevé (vrais IDs de l'ATS) le board ne serait plus « premier » et chaque
    offre déclencherait un faux posting_opened puis un faux posting_closed. Les offres repartent donc du premier
    relevé réel. Renvoie les compteurs, ou {"already_imported": True} si l'import a déjà été fait (marqué par un
    run d'options {"import": "legacy_caches"}).
    """
    if conn.execute("SELECT 1 FROM runs WHERE json_extract(options, '$.import') = 'legacy_caches'").fetchone():
        return {"already_imported": True}
    cache_dir = Path(cache_dir)
    scans, skipped = _cache_lines(cache_dir / "detection.jsonl")
    run_id = start_run(conn, {"import": "legacy_caches", "cache_dir": str(cache_dir)})
    with conn:
        for e in scans:
            _record_scan(conn, e["domain"], e["data"], run_id, e["fetched_at"], DEFAULT_SIGNALS, emit=False)
    stats = {"scans": len(scans), "skipped_lines": skipped}
    finish_run(conn, run_id, sites_read=len(scans), errors=skipped)
    return stats
