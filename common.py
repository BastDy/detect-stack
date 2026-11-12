"""Code partagé par tous les scripts : requêtes web, domaines, cache, chemins.

Stdlib uniquement, pour que le projet tourne sans rien installer.
"""
import csv
import json
import random
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
INPUTS = DATA / "inputs"
SOURCES = DATA / "sources"
CACHE = DATA / "cache"
OUTPUT = DATA / "output"
CONFIG = HERE / "config.json"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
MAX_BYTES = 3_000_000

# Politesse envers les serveurs (le pipeline tourne avec 24 threads) : limites par hôte.
HOST_MAX_CONCURRENT = 4    # requêtes simultanées au plus vers un même hôte
HOST_MIN_INTERVAL = 0.25   # secondes entre deux DÉBUTS de requête vers un même hôte
THROTTLE_RETRIES = 3       # nouvelles tentatives sur 429 / 503
RETRY_AFTER_MAX = 30       # plafond de l'attente (Retry-After ou backoff), en secondes
BACKOFF_BASE = 1.5         # backoff exponentiel : BACKOFF_BASE * 2**essai + gigue

CITY_PROVINCE = {
    **dict.fromkeys(["toronto", "waterloo", "kitchener", "ottawa", "mississauga", "hamilton", "london", "markham",
                     "vaughan", "oakville", "burlington", "guelph", "kingston", "barrie", "richmond hill", "cambridge",
                     "north york", "etobicoke", "scarborough", "brampton", "pickering", "ajax", "whitby", "oshawa"], "Ontario"),
    **dict.fromkeys(["montréal", "montreal", "québec city", "quebec city", "quebec", "sherbrooke", "laval", "gatineau"], "Quebec"),
    **dict.fromkeys(["vancouver", "victoria", "burnaby", "surrey", "kelowna", "richmond"], "British Columbia"),
    **dict.fromkeys(["calgary", "edmonton"], "Alberta"),
    **dict.fromkeys(["winnipeg"], "Manitoba"),
    **dict.fromkeys(["saskatoon", "regina"], "Saskatchewan"),
    **dict.fromkeys(["halifax", "dartmouth"], "Nova Scotia"),
    **dict.fromkeys(["fredericton", "moncton", "saint john"], "New Brunswick"),
    **dict.fromkeys(["st. john's", "st john's"], "Newfoundland and Labrador"),
    **dict.fromkeys(["charlottetown"], "Prince Edward Island"),
}
PROVINCE_CODES = {
    "ON": "Ontario", "QC": "Quebec", "BC": "British Columbia", "AB": "Alberta", "MB": "Manitoba",
    "SK": "Saskatchewan", "NS": "Nova Scotia", "NB": "New Brunswick", "NL": "Newfoundland and Labrador",
    "PE": "Prince Edward Island", "YT": "Yukon", "NT": "Northwest Territories", "NU": "Nunavut",
}


# Catégories d'intitulés de poste : source unique pour le score (score_prospects) et l'historique (db).
REVOPS_TITLE = re.compile(
    r"revenue operations|\brevops\b|sales operations|\bsales ops\b|marketing operations|\bmarketing ops\b"
    r"|gtm operations|go-to-market operations|customer success operations|crm (?:admin|administrator|manager|specialist)|hubspot|salesforce admin",
    re.I,
)
SALES_TITLE = re.compile(
    r"account executive|\bsales\b|business development|\b[bs]dr\b|account manager|marketing|growth|demand gen|customer success",
    re.I,
)
LEADERSHIP_TITLE = re.compile(
    r"\b(?:vp|vice[- ]president)\b.*\b(?:sales|revenue|marketing|growth|customer success)\b"
    r"|\bcro\b|\bcmo\b"
    r"|chief (?:revenue|commercial|marketing|growth|customer) officer"
    r"|head of (?:sales|revenue|marketing|growth|gtm|go-to-market|customer success)"
    r"|director,? (?:of )?(?:sales|revenue|marketing|growth|demand gen\w*|customer success)",
    re.I,
)


def job_category(title):
    """'revops' | 'leadership' | 'sales' | 'other' (priorité dans cet ordre)."""
    title = title or ""
    if REVOPS_TITLE.search(title):
        return "revops"
    if LEADERSHIP_TITLE.search(title):
        return "leadership"
    if SALES_TITLE.search(title):
        return "sales"
    return "other"


def load_config():
    if not CONFIG.exists():
        return {}
    return json.loads(CONFIG.read_text(encoding="utf-8-sig"))


def normalize(value):
    """'https://www.Acme.com/about' -> 'acme.com'. Renvoie '' si ce n'est pas un domaine."""
    value = (value or "").strip().lower()
    if not value or value.startswith("#"):
        return ""
    if "://" not in value:
        value = "//" + value
    host = urlparse(value).netloc.split("@")[-1].split(":")[0].strip(".")
    host = host[4:] if host.startswith("www.") else host
    return host if "." in host else ""


def _transient(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or exc.code >= 500
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        # DNS introuvable = le domaine n'existe pas, inutile de réessayer.
        return isinstance(reason, (TimeoutError, socket.timeout, ConnectionError))
    return isinstance(exc, (TimeoutError, socket.timeout, ConnectionError))


class _Host:
    """État d'un hôte : créneaux simultanés, verrou d'espacement, début de la dernière requête."""

    def __init__(self):
        self.slots = threading.BoundedSemaphore(HOST_MAX_CONCURRENT)
        self.lock = threading.Lock()
        self.last_start = 0.0


_HOSTS = {}
_HOSTS_LOCK = threading.Lock()
_THROTTLED = {}
_THROTTLED_LOCK = threading.Lock()


def _host_state(host):
    with _HOSTS_LOCK:
        state = _HOSTS.get(host)
        if state is None:
            state = _HOSTS[host] = _Host()
        return state


def _acquire(host):
    """Prend un créneau sur l'hôte et espace les débuts de requête. Ne bloque que cet hôte."""
    state = _host_state(host)
    state.slots.acquire()
    try:
        with state.lock:
            wait = state.last_start + HOST_MIN_INTERVAL - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            state.last_start = time.monotonic()
    except BaseException:
        state.slots.release()
        raise
    return state


def throttle_stats():
    """Copie {hôte: nombre de réponses 429/503 reçues} depuis le dernier reset."""
    with _THROTTLED_LOCK:
        return dict(_THROTTLED)


def reset_throttle_stats():
    with _THROTTLED_LOCK:
        _THROTTLED.clear()


def _is_throttle(exc):
    return isinstance(exc, urllib.error.HTTPError) and exc.code in (429, 503)


def _retry_after(exc):
    """Délai demandé par l'en-tête Retry-After (secondes ou date HTTP), plafonné ; None si absent ou illisible."""
    value = (exc.headers.get("Retry-After") if exc.headers else None) or ""
    value = value.strip()
    if not value:
        return None
    try:
        delay = float(int(value))
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            delay = (when - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError):
            return None
    return min(max(delay, 0.0), RETRY_AFTER_MAX)


def _backoff(attempt):
    return min(BACKOFF_BASE * 2 ** attempt + random.uniform(0, 0.5), RETRY_AFTER_MAX)


def fetch(url, timeout, accept=None, max_bytes=MAX_BYTES, retries=1):
    """GET -> (url finale, texte), avec limite par hôte (HOST_MAX_CONCURRENT, HOST_MIN_INTERVAL).

    - 429 / 503 : jusqu'à THROTTLE_RETRIES nouvelles tentatives, en attendant Retry-After (plafonné à
      RETRY_AFTER_MAX) ou un backoff exponentiel avec gigue ; chaque réponse est comptée (throttle_stats) ;
    - autres erreurs passagères (timeout, connexion, 5xx) : `retries` nouvelles tentatives après 1,5 s × essai ;
    - le créneau de l'hôte est libéré avant d'attendre ; DNS introuvable, 404… ne sont pas retentés.
    """
    headers = {"User-Agent": UA, "Accept-Language": "en"}
    if accept:
        headers["Accept"] = accept
    host = (urlparse(url).hostname or "").lower()
    throttled = other = 0
    while True:
        state = _acquire(host)
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.geturl(), resp.read(max_bytes).decode("utf-8", errors="replace")
        except Exception as exc:
            error = exc
        finally:
            state.slots.release()
        if _is_throttle(error):
            with _THROTTLED_LOCK:
                _THROTTLED[host] = _THROTTLED.get(host, 0) + 1
            if throttled >= THROTTLE_RETRIES:
                raise error
            delay = _retry_after(error)
            if delay is None:
                delay = _backoff(throttled)
            throttled += 1
            error.close()
        elif _transient(error) and other < retries:
            delay = 1.5 * (other + 1)
            other += 1
        else:
            raise error
        time.sleep(delay)


def fetch_home(domain, timeout):
    last_error = None
    for url in (f"https://{domain}", f"https://www.{domain}", f"http://{domain}"):
        try:
            return fetch(url, timeout)
        except Exception as exc:
            last_error = exc
    raise last_error


def is_error(data):
    """Résultat en échec : "erreur", HubSpot « Erreur », ou "ok": False (recherche d'offres non concluante)."""
    return bool(data.get("erreur")) or data.get("hubspot") == "Erreur" or data.get("ok") is False


class Cache:
    """Cache JSONL par domaine, avec date d'expiration.

    - une entrée plus vieille que max_age_days est refaite ;
    - une erreur (site injoignable…) n'est gardée qu'error_age_days, pour réessayer au prochain run ;
    - les anciennes entrées sans date (avant novembre 2026) sont considérées comme expirées.
    """

    def __init__(self, path, max_age_days, error_age_days=1, now=None):
        self.path = Path(path)
        self.max_age = timedelta(days=max_age_days)
        self.error_age = min(timedelta(days=error_age_days), self.max_age)
        # Tronqué aux secondes : même précision que l'horodatage écrit par put().
        self.now = (now or datetime.now()).replace(microsecond=0)
        self.entries = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    entry = json.loads(line)
                    self.entries[entry["domain"]] = entry

    def _fresh(self, entry):
        stamp = entry.get("fetched_at")
        if not stamp:
            return False
        age = self.now - datetime.fromisoformat(stamp)
        return age <= (self.error_age if is_error(entry["data"]) else self.max_age)

    def get(self, domain):
        entry = self.entries.get(domain)
        return entry["data"] if entry and self._fresh(entry) else None

    def stale(self, domains):
        return [d for d in domains if self.get(d) is None]

    def put(self, domain, data, out=None):
        entry = {"domain": domain, "fetched_at": self.now.isoformat(timespec="seconds"), "data": data}
        self.entries[domain] = entry
        if out is not None:
            out.write(json.dumps(entry, ensure_ascii=False) + "\n")
            out.flush()

    def writer(self):
        """Fichier ouvert en ajout, passé à put() par run_stage pour sauvegarder au fil de l'eau."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return open(self.path, "a", encoding="utf-8")

    def compact(self):
        """Réécrit le fichier avec une seule entrée par domaine, sans les entrées expirées."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for entry in self.entries.values():
                if self._fresh(entry):
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        tmp.replace(self.path)

    def data(self):
        return {d: e["data"] for d, e in self.entries.items() if self._fresh(e)}


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


def norm_company(name):
    """Nom d'entreprise réduit à [a-z0-9], pour comparer sans tenir compte de la casse ni de la ponctuation."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def run_stage(label, domains, worker, cache, workers):
    """Applique worker aux domaines absents ou expirés du cache, en parallèle, en sauvegardant au fil de l'eau.

    cache : un Cache, ou tout objet exposant stale / put / writer / compact (ex. pipeline.HistoryStore).
    put() est toujours appelé depuis le thread principal (seul worker tourne dans le pool).
    """
    todo = cache.stale(domains)
    print(f"{label} : {len(domains) - len(todo)} déjà à jour, {len(todo)} à traiter", flush=True)
    if todo:
        with cache.writer() as out, ThreadPoolExecutor(max_workers=workers) as pool:
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
