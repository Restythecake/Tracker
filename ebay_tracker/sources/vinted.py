"""Vinted.de: Katalogsuche, neueste zuerst.

Vinted hat keine offene API mehr (/api/v2/catalog/items antwortet 404, Stand
09/2026), die Katalogseite ist aber serverseitig gerendert. Jede Karte hat einen
Overlay-Link mit einem ``title`` wie

    "Nintendo switch 2, Marke: Nintendo, Zustand: Neu, 150.00 €, 158.20 €"

Die zweite Zahl ist der Preis **inkl. Käuferschutz** -- das, was man wirklich
zahlt, also der Preis, mit dem der Tracker rechnet. Versand kommt noch dazu.

Vinted mischt Anzeigen aus anderen Ländern (IT, FR, ...) in die deutschen
Ergebnisse; das ist ok, Versand geht europaweit mit Käuferschutz. Abholung gibt
es nicht, ein Umkreis wäre sinnlos.
"""
from __future__ import annotations

import logging
import re
import time

from bs4 import BeautifulSoup

from ..config import Search, Settings
from ..models import Listing
from .base import SourceError
from .fetch import Fetcher
from .http import REQUEST_ERRORS

log = logging.getLogger(__name__)

BASE = "https://www.vinted.de"
_AMOUNT = re.compile(r"(\d+(?:[.,]\d{1,2})?)\s*€")
_ID = re.compile(r"/items/(\d+)")


def parse_title(title: str) -> tuple[str, str | None, float | None, float | None]:
    """-> (Name, Zustand, Artikelpreis, Preis inkl. Käuferschutz)."""
    amounts = [float(a.replace(",", ".")) for a in _AMOUNT.findall(title)]
    head = _AMOUNT.split(title)[0].rstrip(", ")
    condition = None
    m = re.search(r"Zustand:\s*([^,]+(?:,\s*mit Etikett)?)", head)
    if m:
        condition = m.group(1).strip()
    name = re.split(r",\s*(?:Marke|Zustand|Größe):", head)[0].strip()
    price = amounts[0] if amounts else None
    total = amounts[1] if len(amounts) > 1 else price
    return name, condition, price, total


def parse_results(html: str) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    out: dict[str, Listing] = {}
    for a in soup.select('a[data-testid$="--overlay-link"]'):
        href = a.get("href", "")
        m = _ID.search(href)
        title = a.get("title") or ""
        if not m or not title:
            continue
        item_id = m.group(1)
        if item_id in out:
            continue
        name, condition, price, total = parse_title(title)
        testid = a.get("data-testid", "").removesuffix("--overlay-link")
        img = soup.select_one(f'img[data-testid="{testid}--image--img"]')
        out[item_id] = Listing(
            item_id=item_id,
            title=name,
            price=total,
            price_note="inkl. Käuferschutz" if total and price and total != price else None,
            currency="EUR",
            url=BASE + href.split("?")[0],
            image_url=img.get("src") if img else None,
            condition=condition,
            is_new=condition.lower().startswith("neu") if condition else None,
            buying_options=["FIXED_PRICE"],
            shipping_note="nur Versand",
            source="vinted",
        )
    return list(out.values())


class VintedSource:
    def __init__(self, settings: Settings, session=None):
        self.settings = settings
        self.fetcher = Fetcher("vinted", "Vinted", mode=settings.fetcher_for("vinted"), session=session,
                               accept_language="de-DE,de;q=0.9")
        # Für welche Session die Cookies schon geholt sind (nach einer Sperre gibt es eine neue)
        self._warm_for = self.fetcher.session if session is not None else None

    def _warm_up(self) -> None:
        # Die Startseite setzt anon_id/access_token_web; ohne sie liefert der
        # Katalog eine leere Hülle.
        session = self.fetcher.session
        if self._warm_for is not session:
            try:
                session.get(BASE + "/", timeout=20)
            except REQUEST_ERRORS as exc:
                log.debug("Vinted-Warmup: %s", exc)
            self._warm_for = session

    def search(self, search: Search) -> list[Listing]:
        results: dict[str, Listing] = {}
        for i, query in enumerate(search.all_queries):
            if i:
                time.sleep(1.5)
            self._warm_up()
            params = {"search_text": query, "order": "newest_first"}
            if search.min_price is not None:
                params["price_from"] = f"{search.min_price:.0f}"
            if search.max_price is not None:
                params["price_to"] = f"{search.max_price:.0f}"
            page = self.fetcher.get(BASE + "/catalog", params=params)
            if page.status != 200:
                raise SourceError(f"Vinted-Suche fehlgeschlagen ({page.status})")
            for listing in parse_results(page.text):
                results.setdefault(listing.item_id, listing)
        return list(results.values())[: self.settings.max_results]
