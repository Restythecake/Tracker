"""Kleinanzeigen.de: liest die normale Suchergebnisseite, sortiert nach "Neueste".

Es gibt keine offizielle API. Die Seite ist serverseitig gerendert (Astro, Stand
09/2026); jede Anzeige ist ein ``<article data-adid=… data-href=…>`` mit Titel in
``h3 a``, Preis als ``<p>… €</p>`` und Ort in einem ``<span>`` neben dem Pin-Icon.
Der Parser hält sich an diese Attribute statt an CSS-Klassen, weil die Klassen
Tailwind-Utilities sind und sich bei jedem Redesign ändern.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup, Tag

from ..config import Search, Settings
from ..models import Listing
from .base import SourceError
from .http import REQUEST_ERRORS, new_session
from .scraper import parse_price

log = logging.getLogger(__name__)

BASE = "https://www.kleinanzeigen.de"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/18.0 Safari/605.1.15"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9",
}

_PLZ = re.compile(r"^\d{5}$")
_RADII = (5, 10, 20, 30, 50, 100, 150, 200)
_WS = re.compile(r"\s+")
# "469 €", "1.250 € VB", "VB", "Zu verschenken", auch "450 € 395 €" (reduziert)
_PRICE_ONLY = re.compile(r"(?:(?:\d[\d.]*(?:,\d{1,2})?\s*€\s*)+(?:VB)?|VB|Zu verschenken)")


def slugify_query(query: str) -> str:
    """'ThinkPad T14 32GB' -> 'thinkpad-t14-32gb' (Umlaute bleiben, werden URL-kodiert)."""
    words = [w for w in re.split(r"[\s/]+", query.strip().lower()) if w]
    return quote("-".join(words), safe="-")


def snap_radius(km: int) -> int:
    """Kleinanzeigen kennt nur feste Umkreise; nimm den nächstgrößeren."""
    for r in _RADII:
        if km <= r:
            return r
    return _RADII[-1]


def build_url(search: Search, location_id: str | None = None, plz: str | None = None,
              radius_km: int | None = None) -> str:
    segments: list[str] = []
    # "anzeige:angebote" blendet Gesuche aus ("Suche ThinkPad T14") -- die sind
    # für einen Tracker, der Angebote melden soll, immer Rauschen.
    filters = ["anzeige:angebote", "sortierung:neuste"]
    if search.min_price is not None or search.max_price is not None:
        lo = f"{search.min_price:.0f}" if search.min_price is not None else ""
        hi = f"{search.max_price:.0f}" if search.max_price is not None else ""
        filters.append(f"preis:{lo}:{hi}")
    if location_id and plz:
        segments.append(f"s-{plz}")
        segments.extend(filters)
        code = f"k0l{location_id}"
        if radius_km:
            code += f"r{snap_radius(radius_km)}"
    else:
        segments.append("s-" + filters[0])
        segments.extend(filters[1:])
        code = "k0"
    segments.append(slugify_query(search.query))
    segments.append(code)
    return f"{BASE}/" + "/".join(segments)


def _text(el: Tag | None) -> str:
    return _WS.sub(" ", el.get_text(" ", strip=True)).strip() if el else ""


def _price(card: Tag) -> tuple[float | None, str | None]:
    """'469 €' / '469 € VB' / 'VB' / 'Zu verschenken' -> (Preis, Notiz)."""
    for p in card.find_all("p"):
        text = _text(p)
        if "€" in text or re.fullmatch(r"VB|Zu verschenken", text):
            note = None
            if "VB" in text:
                note = "VB"
            if "verschenken" in text.lower():
                return 0.0, "Zu verschenken"
            # Reduziert zeigt die Seite Alt- und Neupreis; der kleinere ist der aktuelle
            amounts = [parse_price(a) for a in re.findall(r"\d[\d.]*(?:,\d{1,2})?", text)]
            amounts = [a for a in amounts if a is not None]
            return (min(amounts) if amounts else None), note
    return None, None


def _location(card: Tag) -> str | None:
    for span in card.find_all("span"):
        text = _text(span)
        if re.match(r"^\d{5}\s+\S", text):
            return text
    return None


def parse_results(html: str) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    listings: dict[str, Listing] = {}
    for article in soup.select("article[data-adid]"):
        ad_id = article.get("data-adid", "").strip()
        href = article.get("data-href", "")
        if not ad_id or not href or ad_id in listings:
            continue
        title_el = article.select_one("h3 a") or article.select_one("h2 a")
        title = _text(title_el)
        if not title:
            continue
        price, note = _price(article)
        img = article.select_one("[data-image-container] img") or article.find("img")
        image = img.get("src") if img else None
        listings[ad_id] = Listing(
            item_id=ad_id,
            title=title,
            price=price,
            price_note=note,
            currency="EUR",
            url=BASE + href if href.startswith("/") else href,
            image_url=image,
            location=_location(article),
            buying_options=["FIXED_PRICE"],
            source="kleinanzeigen",
        )
    return list(listings.values())


class KleinanzeigenSource:
    def __init__(self, settings: Settings, session: requests.Session | None = None):
        self.settings = settings
        if session is not None:
            self.session = session
            self.session.headers.update(HEADERS)
        else:
            self.session = new_session("de-DE,de;q=0.9")
        self._locations: dict[str, str | None] = {}

    def location_id(self, plz: str) -> str | None:
        """PLZ -> Kleinanzeigen-Orts-ID über die Autocomplete-Schnittstelle der Seite."""
        if plz in self._locations:
            return self._locations[plz]
        loc = None
        try:
            resp = self.session.get(f"{BASE}/s-ort-empfehlungen.json", params={"query": plz}, timeout=15)
            if resp.ok:
                for key in resp.json():
                    # "_0" ist "Deutschland", die echten Orte haben eine Zahl als ID
                    if key.startswith("_") and key[1:].isdigit() and key != "_0":
                        loc = key[1:]
                        break
        except (*REQUEST_ERRORS, ValueError) as exc:
            log.warning("Kleinanzeigen-Ort für PLZ %s nicht auflösbar: %s", plz, exc)
            return None  # nicht cachen, nächster Lauf versucht es erneut
        self._locations[plz] = loc
        return loc

    def build_url(self, search: Search) -> str:
        plz = search.plz or self.settings.kleinanzeigen_plz
        radius = search.radius_km or self.settings.kleinanzeigen_radius_km
        if plz and _PLZ.match(plz):
            loc = self.location_id(plz)
            if loc:
                return build_url(search, loc, plz, radius)
            log.warning("PLZ %s unbekannt – suche deutschlandweit", plz)
        return build_url(search)

    def search(self, search: Search) -> list[Listing]:
        url = self.build_url(search)
        try:
            resp = self.session.get(url, timeout=30)
        except REQUEST_ERRORS as exc:
            raise SourceError(f"Kleinanzeigen nicht erreichbar: {exc.__class__.__name__}") from exc
        if resp.status_code in (403, 429):
            raise SourceError(f"Kleinanzeigen blockt die Anfrage ({resp.status_code}) – Intervall erhöhen")
        if resp.status_code != 200:
            raise SourceError(f"Kleinanzeigen-Suche fehlgeschlagen ({resp.status_code})")
        # Die Seite ist UTF-8, sagt es aber nicht im Content-Type; requests rät
        # dann Latin-1 und aus "469 €" wird "469 â\x82¬" -- kein Preis erkannt.
        html = resp.content.decode("utf-8", errors="replace")
        listings = parse_results(html)
        if not listings and "data-adid" not in html and "Keine Ergebnisse" not in html:
            log.warning("Kleinanzeigen: keine Anzeigen erkannt – evtl. hat sich das Seitenlayout geändert")
        return listings[: self.settings.max_results]
