"""mydealz.de: Deals der Community, per Suche, neueste zuerst.

Die RSS-Feeds (``/rss/new``, ``/rss/hot``) kennen keine Suche -- nur die
30 neuesten Deals aller Kategorien; eine Suche nach "ddr5 64gb" fände dort fast
nie etwas. Die Suchseite dagegen ist serverseitig gerendert und trägt jeden
Deal als JSON im Attribut ``data-vue3`` (Komponente
``ThreadMainListItemNormalizer``): Titel, Preis, Händler, Versand, abgelaufen
ja/nein. Das ist Pepper-Plattform-Datenmodell statt CSS-Klassen, also deutlich
stabiler als HTML-Parsing -- und braucht keinen Browser (Stand 28.09.2026:
curl_cffi bekommt 200).

Abgelaufene Deals werden verworfen; es gibt keinen Ort und keine Detailseite.
Händlerangebote sind Neuware, die Bewertung (bewertung:pc/gpu/ram) liest wie
überall nur den Titel.
"""
from __future__ import annotations

import html as htmllib
import json
import logging
import re
import time

from ..config import Search, Settings
from ..models import Listing
from .base import SourceError
from .fetch import Fetcher

log = logging.getLogger(__name__)

BASE = "https://www.mydealz.de"
IMAGE_BASE = "https://static.mydealz.de"
_PROPS = re.compile(r"data-vue3='([^']*)'")
_USED = re.compile(r"\b(gebraucht|refurbished|generalüberholt|b-ware|aussteller)\b", re.IGNORECASE)


def _image(img: dict | None) -> str | None:
    if not img or not img.get("path") or not img.get("name"):
        return None
    # uid endet oft auf ".raw"; ausgeliefert wird das Bild als JPEG (so auch im RSS)
    return f"{IMAGE_BASE}/{img['path']}/{img['name']}/re/300x300/qt/60/{img['name']}.jpg"


def title_matches(title: str, query: str) -> bool:
    """mydealz sucht auch in Beschreibung und Kommentaren ("rtx 4070" fand ein
    RC-Auto). Der Tracker meldet nur, was im Titel steht: jedes Suchwort muss drin sein."""
    low = title.lower()
    return all(word in low for word in query.lower().split())


def parse_results(page: str) -> list[Listing]:
    out: dict[str, Listing] = {}
    for raw in _PROPS.findall(page):
        try:
            comp = json.loads(htmllib.unescape(raw))
        except ValueError:
            continue
        if comp.get("name") != "ThreadMainListItemNormalizer":
            continue
        t = (comp.get("props") or {}).get("thread") or {}
        tid, title = str(t.get("threadId") or ""), (t.get("title") or "").strip()
        if not tid or not title or tid in out or t.get("isExpired") or t.get("type") != "Deal":
            continue
        price = t.get("price")
        shipping = (t.get("shipping") or {}).get("price")
        merchant = (t.get("merchant") or {}).get("merchantName") or t.get("linkHost")
        used = bool(_USED.search(title))
        out[tid] = Listing(
            item_id=tid,
            title=title,
            price=float(price) if isinstance(price, (int, float)) and price > 0 else None,
            currency="EUR",
            url=f"{BASE}/deals/{t.get('titleSlug') or 'deal'}-{tid}",
            image_url=_image(t.get("mainImage")),
            condition="Gebraucht" if used else None,
            is_new=False if used else None,
            buying_options=["FIXED_PRICE"],
            shipping=float(shipping) if isinstance(shipping, (int, float)) and shipping > 0 else None,
            source="mydealz",
            dealer=True,
            shipping_note=f"bei {merchant}" if merchant else None,
        )
    return list(out.values())


class MydealzSource:
    def __init__(self, settings: Settings, session=None):
        self.settings = settings
        self.fetcher = Fetcher("mydealz", "mydealz", mode=settings.fetcher_for("mydealz"), session=session,
                               accept_language="de-DE,de;q=0.9")

    def search(self, search: Search) -> list[Listing]:
        results: dict[str, Listing] = {}
        for i, query in enumerate(search.all_queries):
            if i:
                time.sleep(1.5)
            page = self.fetcher.get(f"{BASE}/search", params={"q": query, "sortBy": "new", "hide_expired": "true"})
            if page.status != 200:
                raise SourceError(f"mydealz-Suche fehlgeschlagen ({page.status})")
            found = parse_results(page.text)
            if not found and "data-vue3" not in page.text:  # leere Suche hat trotzdem Komponenten
                log.warning("mydealz: keine Deals erkannt – evtl. hat sich die Seite geändert")
            for listing in found:
                if title_matches(listing.title, query):
                    results.setdefault(listing.item_id, listing)
        return list(results.values())[: self.settings.max_results]
