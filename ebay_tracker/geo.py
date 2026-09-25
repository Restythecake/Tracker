"""Entfernung PLZ -> PLZ und was die Abholung kostet.

Koordinaten je Postleitzahl kommen von Nominatim (OpenStreetMap), einmal je PLZ
und dann für immer gecacht -- Nominatim erlaubt 1 Anfrage/Sekunde und verlangt
einen eigenen User-Agent. Luftlinie x 1,25 ist eine gute Näherung für die
Straßenstrecke in Deutschland.

Fahrtkosten: hin und zurück, 0,25 €/km (Sprit ~7 l/100 km plus Verschleiß).
Das ist der Betrag, der aus einem "600 € in 190 km" gegenüber "650 € um die
Ecke" plötzlich den schlechteren Deal macht.
"""
from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
import urllib.parse
import urllib.request
from typing import Callable

log = logging.getLogger(__name__)

ROAD_FACTOR = 1.25
EUR_PER_KM = 0.25
_PLZ = re.compile(r"\b(\d{5})\b")


def plz_of(location: str | None) -> str | None:
    m = _PLZ.search(location or "")
    return m.group(1) if m else None


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


class Geocoder:
    """PLZ -> (lat, lon), mit dauerhaftem Cache über get/set-Callbacks."""

    def __init__(self, cache_get: Callable[[str], object], cache_set: Callable[[str, object], None],
                 fetch: Callable[[str], tuple[float, float] | None] | None = None):
        self._get = cache_get
        self._set = cache_set
        self._fetch = fetch or self._nominatim
        self._lock = threading.Lock()
        self._last = 0.0

    def _nominatim(self, plz: str) -> tuple[float, float] | None:
        with self._lock:
            wait = 1.1 - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
        q = urllib.parse.urlencode({"postalcode": plz, "country": "de", "format": "json", "limit": 1})
        req = urllib.request.Request(f"https://nominatim.openstreetmap.org/search?{q}",
                                     headers={"User-Agent": "nest-tracker/0.5 (homelab deal tracker)"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
        except Exception as exc:  # Netz weg: kein Cache-Eintrag, nächster Lauf versucht es erneut
            log.debug("Nominatim %s: %s", plz, exc)
            return None
        if not data:
            return (0.0, 0.0)  # unbekannte PLZ -- merken, nicht jedes Mal fragen
        return float(data[0]["lat"]), float(data[0]["lon"])

    def coords(self, plz: str) -> tuple[float, float] | None:
        cached = self._get(f"geo:{plz}")
        if cached is not None:
            return None if cached == [0.0, 0.0] else tuple(cached)  # type: ignore[return-value]
        got = self._fetch(plz)
        if got is None:
            return None
        self._set(f"geo:{plz}", list(got))
        return None if got == (0.0, 0.0) else got

    def road_km(self, home: str, other: str) -> int | None:
        a, b = self.coords(home), self.coords(other)
        if not a or not b:
            return None
        return int(round(haversine_km(a, b) * ROAD_FACTOR))


def trip_cost(km: int) -> int:
    return int(round(2 * km * EUR_PER_KM, -1))
