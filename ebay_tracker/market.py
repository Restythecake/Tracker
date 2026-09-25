"""Echte Marktpreise je Grafikkarte statt geschätzter Tabellenwerte.

Quelle 1 (bevorzugt): **verkaufte** eBay-Angebote der letzten Wochen
(``LH_Sold=1&LH_Complete=1``, Kategorie Grafikkarten 27386, gebraucht). Das ist
der Preis, zu dem Karten wirklich den Besitzer wechseln.

Quelle 2 (Ersatz, wenn eBay sperrt): Kleinanzeigen-**Angebots**preise einzelner
Karten, mit Abschlag 0,9 -- Angebotspreise liegen erfahrungsgemäß über dem,
wofür am Ende verkauft wird.

Robust gegen Müll: Nur Anzeigen, deren erkannte GPU exakt die gesuchte ist,
keine PCs/Laptops/defekten Karten/leeren Kartons, Ausreißer über den
Interquartilsabstand entfernt, mindestens 5 Preise. Sonst bleibt der
Tabellenwert aus ``hardware.GPUS``.
"""
from __future__ import annotations

import logging
import re
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from .hardware import GPUS, _norm, find_gpu
from .models import Listing
from .sources.http import REQUEST_ERRORS, new_session

log = logging.getLogger(__name__)

MAX_AGE_HOURS = 24
MIN_SAMPLES = 5
JUNK = re.compile(r"defekt|bastler|kaputt|leer|karton|nur\s*box|ovp\s*only|gaming\s*pc|\bpc\b|laptop|notebook|"
                  r"tausch|suche|kaufe|kühler|kühlkörper|keine\s*karte|heatsink|backplate|wasserk(?:ü|ue)hl|block\b|halterung", re.I)

NAME_TO_KEY = {v[0]: k for k, v in GPUS.items()}


@dataclass
class MarketPrice:
    gpu: str
    median: float
    n: int
    source: str  # "ebay-verkauft" | "kleinanzeigen-angebote"
    at: str

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def clean_prices(listings: list[Listing], gpu_key: str) -> list[float]:
    table = GPUS[gpu_key][2]
    prices = []
    for l in listings:
        if not l.price or JUNK.search(l.title):
            continue
        found = find_gpu(_norm(l.title))
        if not found or found[0] != gpu_key:
            continue
        if not 0.3 * table <= l.price <= 2.5 * table:
            continue
        prices.append(float(l.price))
    if len(prices) >= 8:
        q = statistics.quantiles(prices, n=4)
        iqr = q[2] - q[0]
        prices = [p for p in prices if q[0] - 1.5 * iqr <= p <= q[2] + 1.5 * iqr]
    return prices


def fetch_ebay_sold(session, gpu_name: str) -> list[Listing]:
    from .sources.scraper import parse_results
    resp = session.get("https://www.ebay.de/sch/i.html", timeout=25, params={
        "_nkw": gpu_name, "_sacat": "27386", "LH_Sold": "1", "LH_Complete": "1",
        "LH_ItemCondition": "3000", "_ipg": "120",
    })
    if resp.status_code != 200 or "challenge" in str(resp.url):
        raise PermissionError(f"eBay {resp.status_code}")
    return parse_results(resp.content.decode("utf-8", errors="replace"), "EUR")


def fetch_ka_asking(session, gpu_name: str) -> list[Listing]:
    from .sources.kleinanzeigen import parse_results, slugify_query
    # c225 = PC-Zubehör & Software; darin landen einzelne Grafikkarten
    url = f"https://www.kleinanzeigen.de/s-pc-zubehoer-software/anzeige:angebote/{slugify_query(gpu_name)}/k0c225"
    resp = session.get(url, timeout=25)
    if resp.status_code != 200:
        raise PermissionError(f"Kleinanzeigen {resp.status_code}")
    return parse_results(resp.content.decode("utf-8", errors="replace"))


class Market:
    """Hält Marktpreise je GPU-Name, gecacht über get/set-Callbacks (z. B. Store-Meta)."""

    def __init__(self, cache_get: Callable[[str], object], cache_set: Callable[[str, object], None],
                 session=None, sleep: Callable[[float], None] = time.sleep):
        self._get = cache_get
        self._set = cache_set
        self._session = session
        self._sleep = sleep

    @property
    def session(self):
        if self._session is None:
            self._session = new_session("de-DE,de;q=0.9")
        return self._session

    def get(self, gpu_name: str) -> MarketPrice | None:
        raw = self._get(f"market:{gpu_name}")
        return MarketPrice(**raw) if isinstance(raw, dict) else None

    def fresh(self, gpu_name: str) -> bool:
        mp = self.get(gpu_name)
        if mp is None:
            return False
        age = datetime.now(timezone.utc) - datetime.fromisoformat(mp.at)
        return age.total_seconds() < MAX_AGE_HOURS * 3600

    def refresh(self, gpu_name: str, *, ebay_blocked: bool = False) -> MarketPrice | None:
        key = NAME_TO_KEY.get(gpu_name)
        if key is None:
            return None
        attempts: list[tuple[str, Callable, float]] = []
        if not ebay_blocked:
            attempts.append(("ebay-verkauft", fetch_ebay_sold, 1.0))
        attempts.append(("kleinanzeigen-angebote", fetch_ka_asking, 0.9))
        for source, fetch, factor in attempts:
            try:
                prices = clean_prices(fetch(self.session, gpu_name), key)
            except (PermissionError, *REQUEST_ERRORS) as exc:
                log.info("Marktpreis %s über %s nicht verfügbar: %s", gpu_name, source, exc)
                continue
            if len(prices) >= MIN_SAMPLES:
                mp = MarketPrice(gpu=gpu_name, median=round(statistics.median(prices) * factor, 2), n=len(prices),
                                 source=source, at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
                self._set(f"market:{gpu_name}", mp.to_dict())
                log.info("Marktpreis %s: %.0f € (%s, n=%d)", gpu_name, mp.median, source, mp.n)
                return mp
        return None

    def refresh_stale(self, gpu_names: set[str], *, limit: int = 3, ebay_blocked: bool = False) -> int:
        """Höchstens ``limit`` veraltete GPUs pro Lauf -- Marktplätze nicht fluten."""
        done = 0
        for name in sorted(gpu_names):
            if done >= limit:
                break
            if self.fresh(name):
                continue
            if done:
                self._sleep(2.0)
            self.refresh(name, ebay_blocked=ebay_blocked)
            done += 1
        return done

    def overrides(self, gpu_names: set[str] | None = None) -> dict[str, float]:
        """GPU-Name -> Marktpreis für alle bekannten Einträge."""
        out = {}
        for name in (gpu_names or set(NAME_TO_KEY)):
            mp = self.get(name)
            if mp:
                out[name] = mp.median
        return out
