"""Code partagé par tous les scripts : requêtes web, domaines, cache, chemins.

Stdlib uniquement, pour que le projet tourne sans rien installer.
"""
import json
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
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


def fetch(url, timeout, accept=None, max_bytes=MAX_BYTES, retries=1):
    """GET -> (url finale, texte). Réessaie une fois sur les erreurs passagères (timeout, 429, 5xx)."""
    headers = {"User-Agent": UA, "Accept-Language": "en"}
    if accept:
        headers["Accept"] = accept
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.geturl(), resp.read(max_bytes).decode("utf-8", errors="replace")
        except Exception as exc:
            if attempt == retries or not _transient(exc):
                raise
            time.sleep(1.5 * (attempt + 1))


def fetch_home(domain, timeout):
    last_error = None
    for url in (f"https://{domain}", f"https://www.{domain}", f"http://{domain}"):
        try:
            return fetch(url, timeout)
        except Exception as exc:
            last_error = exc
    raise last_error


def is_error(data):
    return bool(data.get("erreur")) or data.get("hubspot") == "Erreur"


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
