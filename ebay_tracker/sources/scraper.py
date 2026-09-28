"""Fallback ohne API-Keys: liest die normale eBay-Suchergebnisseite (sortiert nach 'Neu eingestellt').

Funktioniert ohne Anmeldung, ist aber anfälliger: eBay ändert gelegentlich das HTML
und kann bei zu vielen Abfragen ein Captcha zeigen. Für Dauerbetrieb lieber die API nutzen.
"""
from __future__ import annotations

import logging
import re
import time

import requests
from bs4 import BeautifulSoup, Tag

from ..config import Search, Settings
from ..models import Listing
from . import parsing
from .base import SourceError
from .fetch import Fetcher
from .http import HAS_CFFI, REQUEST_ERRORS

log = logging.getLogger(__name__)

# Ohne die Sec-Fetch-Header und ohne vorher geholte Cookies antwortet eBay
# (Stand 09/2026) auf die Suchseite mit 403 -- nachgemessen: gleicher User-Agent,
# nur mit diesen Headern plus einem Besuch der Startseite gibt es 200 und Treffer.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/18.0 Safari/605.1.15"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Dest": "document",
}

_ITEM_ID = re.compile(r"/itm/(?:[^/?#]+/)?(\d{9,15})")
# (?!\d): ohne den Anker las die erste Alternative "1234.56" als 123.
_NUMBER = re.compile(r"\d{1,3}(?:[.,  ]\d{3})*(?:[.,]\d{1,2})?(?!\d)|\d+(?:[.,]\d{1,2})?")
_TITLE_PREFIXES = ("Neues Angebot", "New listing", "Neu eingestellt")


def parse_price(text: str | None) -> float | None:
    """'EUR 1.234,56' / '1,234.56 $' / 'EUR 10,00 bis EUR 20,00' -> 1234.56 / 1234.56 / 10.0"""
    if not text:
        return None
    match = _NUMBER.search(text)
    if not match:
        return None
    raw = match.group(0).replace(" ", "").replace(" ", "")
    last_sep = max(raw.rfind(","), raw.rfind("."))
    if last_sep != -1 and len(raw) - last_sep - 1 in (1, 2):
        integer, decimals = raw[:last_sep], raw[last_sep + 1 :]
    else:
        integer, decimals = raw, "0"
    integer = integer.replace(".", "").replace(",", "")
    try:
        return float(f"{integer}.{decimals}")
    except ValueError:
        return None


def _text(el: Tag | None) -> str:
    return el.get_text(" ", strip=True) if el else ""


def _find_by_class(card: Tag, *needles: str) -> Tag | None:
    for needle in needles:
        el = card.find(class_=lambda c, n=needle: bool(c) and n in c)
        if el:
            return el
    return None


def _clean_title(title: str) -> str:
    for prefix in _TITLE_PREFIXES:
        if title.startswith(prefix):
            title = title[len(prefix) :]
    return title.replace("Wird in neuem Fenster oder Tab geöffnet", "").strip(" -|")


def _shipping(text: str) -> float | None:
    if re.search(r"kostenlos|gratis|free", text, re.IGNORECASE):
        return 0.0
    return parse_price(text)


_AUCTION = re.compile(r"\bGebote?\b|\bbids?\b", re.IGNORECASE)
_CARD_CSS = "ul.srp-results > li.s-card, ul.srp-results > li.s-item"


def parse_results(html: str, currency: str) -> list[Listing]:
    """Trefferkarten lesen: mit Scrapling (selbstheilend), ohne per BeautifulSoup.

    Findet Scrapling gar keine Karte -- auch nicht per Fingerabdruck --, bleibt
    als letzter Versuch der alte Weg über jeden ``/itm/``-Link der Seite."""
    page = parsing.page(html, "https://www.ebay.de")
    if page is None:
        return _parse_results_bs4(html, currency)
    cards, _ = parsing.cards(page, _CARD_CSS, "ebay-card", may_heal="srp-results" in html)
    listings: dict[str, Listing] = {}
    for card in cards:
        link = card.css('a[href*="/itm/"]').first
        match = _ITEM_ID.search(link.attrib.get("href", "")) if link is not None else None
        if not match:
            continue
        item_id = match.group(1)
        if item_id in listings or item_id.startswith("123456"):  # Platzhalter 'Shop on eBay'
            continue
        title_el = _first(card, '[class*="s-card__title"]', '[class*="s-item__title"]') or link
        title = parsing.text(title_el)
        # 'Neues Angebot'-Badge steckt oft in einem eigenen span
        for badge in title_el.css(".LIGHT_HIGHLIGHT, .s-card__new-listing"):
            title = title.replace(parsing.text(badge), "", 1)
        title = _clean_title(title.strip())
        if not title or title.lower().startswith("shop on ebay"):
            continue
        shipping_el = _first(card, '[class*="s-item__shipping"]', '[class*="s-item__logisticsCost"]')
        shipping_text = parsing.text(shipping_el)
        if shipping_el is None:
            shipping_text = next((parsing.text(el) for el in card.css("span, div")
                                  if "Versand" in parsing.text(el) and "EUR" in parsing.text(el)), "")
        img = card.css("img").first
        listings[item_id] = Listing(
            item_id=item_id,
            title=title,
            price=parse_price(parsing.text(_first(card, '[class*="s-card__price"]', '[class*="s-item__price"]'))),
            currency=currency,
            url=f"https://{_domain_from_href(link.attrib.get('href', ''))}/itm/{item_id}",
            image_url=(img.attrib.get("src") or img.attrib.get("data-src")) if img is not None else None,
            buying_options=["AUCTION"] if _AUCTION.search(parsing.text(card)) else ["FIXED_PRICE"],
            shipping=_shipping(shipping_text),
            source="ebay",
        )
    if not cards:
        return _parse_results_bs4(html, currency)
    return list(listings.values())


def _first(card, *selectors: str):
    for css in selectors:
        el = card.css(css).first
        if el is not None:
            return el
    return None


def _parse_results_bs4(html: str, currency: str) -> list[Listing]:
    """Fallback ohne scrapling (schlankes Image) und für Seiten ohne erkennbare Karten."""
    soup = BeautifulSoup(html, "html.parser")
    results_root = soup.select_one("ul.srp-results") or soup
    listings: dict[str, Listing] = {}

    for link in results_root.select('a[href*="/itm/"]'):
        match = _ITEM_ID.search(link.get("href", ""))
        if not match:
            continue
        item_id = match.group(1)
        if item_id in listings or item_id.startswith("123456"):  # Platzhalter 'Shop on eBay'
            continue
        card = link.find_parent("li") or link.parent
        if card is None:
            continue

        title_el = _find_by_class(card, "s-card__title", "s-item__title") or link
        # 'Neues Angebot'-Badge steckt oft in einem eigenen span
        for badge in title_el.select(".LIGHT_HIGHLIGHT, .s-card__new-listing"):
            badge.decompose()
        title = _clean_title(_text(title_el))
        if not title or title.lower().startswith("shop on ebay"):
            continue

        price_el = _find_by_class(card, "s-card__price", "s-item__price")
        shipping_el = _find_by_class(card, "s-item__shipping", "s-item__logisticsCost")
        shipping_text = _text(shipping_el)
        if not shipping_el:
            shipping_text = next(
                (_text(el) for el in card.find_all(["span", "div"]) if "Versand" in _text(el) and "EUR" in _text(el)),
                "",
            )
        shipping = _shipping(shipping_text)
        is_auction = bool(_AUCTION.search(_text(card)))
        img = card.find("img")

        listings[item_id] = Listing(
            item_id=item_id,
            title=title,
            price=parse_price(_text(price_el)),
            currency=currency,
            url=f"https://{_domain_from_href(link.get('href', ''))}/itm/{item_id}",
            image_url=(img.get("src") or img.get("data-src")) if img else None,
            buying_options=["AUCTION"] if is_auction else ["FIXED_PRICE"],
            shipping=shipping,
            source="ebay",
        )
    return list(listings.values())


def _domain_from_href(href: str) -> str:
    m = re.match(r"https?://([^/]+)", href)
    return m.group(1) if m else "www.ebay.de"


CATEGORIES = {"pc": "179"}  # Computer, Tablets & Netzwerk > Desktops & All-in-Ones


class EbayScraperSource:
    def __init__(self, settings: Settings, session: requests.Session | None = None):
        self.settings = settings
        if session is not None:  # Tests reichen eine Fake-Session rein
            session.headers.update(HEADERS)
        self.fetcher = Fetcher("ebay", "eBay", mode=settings.fetcher_for("ebay"), session=session,
                               accept_language=HEADERS["Accept-Language"])
        # Mit curl_cffi passt der Fingerprint bereits; der Startseitenbesuch
        # kostet nur eine weitere Anfrage, die selbst 403 bekommen kann.
        self._warmed_up = session is not None or HAS_CFFI

    @property
    def session(self):
        return self.fetcher.session

    def _warm_up(self) -> None:
        """Startseite einmal besuchen, damit eBay die Session-Cookies setzt (nur requests-Fallback)."""
        if self._warmed_up:
            return
        try:
            self.session.get(f"https://{self.settings.domain}/", timeout=20)
        except REQUEST_ERRORS as exc:  # die Suche selbst meldet den echten Fehler
            log.debug("eBay-Warmup fehlgeschlagen: %s", exc)
        self._warmed_up = True

    def build_params(self, search: Search, query: str | None = None) -> dict[str, str]:
        params = {
            "_nkw": query or search.query,
            "_sop": "10",  # Neu eingestellt zuerst
            "_ipg": "60" if self.settings.max_results <= 60 else "120",
        }
        if search.min_price is not None:
            params["_udlo"] = f"{search.min_price:g}"
        if search.max_price is not None:
            params["_udhi"] = f"{search.max_price:g}"
        if search.condition == "new":
            params["LH_ItemCondition"] = "1000"
        elif search.condition == "used":
            params["LH_ItemCondition"] = "3000"
        if search.buying == "fixed":
            params["LH_BIN"] = "1"
        elif search.buying == "auction":
            params["LH_Auction"] = "1"
        category = search.category_id or CATEGORIES.get(search.category or "")
        if category:
            params["_sacat"] = category
        return params

    def search(self, search: Search) -> list[Listing]:
        results: dict[str, Listing] = {}
        for i, query in enumerate(combine_queries(search.all_queries)):
            if i:
                time.sleep(2.5)
            for listing in self._search_one(search, query):
                results.setdefault(listing.item_id, listing)
        return list(results.values())

    def _get(self, url: str, params: dict | None = None) -> str:
        """Seite holen (bei Sperre per Browser, siehe fetch.py) und als Text liefern."""
        self._warm_up()
        page = self.fetcher.get(url, params=params)
        if page.status != 200:
            raise SourceError(f"eBay-Suche fehlgeschlagen ({page.status})")
        return page.text

    def _search_one(self, search: Search, query: str) -> list[Listing]:
        html = self._get(f"https://{self.settings.domain}/sch/i.html", params=self.build_params(search, query))
        listings = parse_results(html, self.settings.currency)
        if not listings and "srp-results" not in html:
            log.warning("Keine Ergebnisse gefunden – evtl. hat eBay das Seitenlayout geändert")
        return listings[: self.settings.max_results]

    def load_details(self, listing: Listing) -> None:
        """Artikelmerkmale und Verkäuferbeschreibung (liegt in einem iframe auf itm.ebaydesc.com)."""
        desc_url = parse_details(self._get(listing.url), listing)
        if desc_url:
            try:
                time.sleep(1)
                d = self.session.get(desc_url, timeout=20)
                if d.status_code == 200:
                    text = BeautifulSoup(d.content.decode("utf-8", errors="replace"), "html.parser").get_text("\n", strip=True)
                    listing.description = ((listing.description or "") + "\n" + text)[:6000]
            except REQUEST_ERRORS as exc:
                log.debug("eBay-Beschreibung nicht ladbar: %s", exc)


def parse_details(html: str, listing: Listing) -> str | None:
    """Füllt Zustand/Merkmale; gibt die URL der Beschreibung zurück."""
    soup = BeautifulSoup(html, "html.parser")
    pairs = []
    for dl in soup.select("dl.ux-layout-section-evo__item"):
        label = _text(dl.find("dt"))
        value = _text(dl.find("dd"))
        if label and value and not label.startswith("Artikelzustand") and len(value) < 200:
            pairs.append(f"{label}: {value}")
    if pairs:
        listing.description = "\n".join(pairs)
    cond = _text(soup.select_one(".x-item-condition-text"))
    if cond:
        listing.condition = cond.split(" ")[0]
        listing.is_new = listing.condition.lower() == "neu"
    listing.details_loaded = True
    frame = soup.select_one("iframe#desc_ifr")
    src = frame.get("src") if frame else None
    return src if src and src.startswith("https://itm.ebaydesc.com/") else None


# ------------------------------------------------------------ ODER-Suchen
MAX_OR_TERMS = 10
_MODEL_NUMBER = re.compile(r"(?<!\S)\d{3,5}(?!\S)")  # "3090", "7900" -- nicht "96gb", "ddr4"


def combine_queries(queries: list[str]) -> list[str]:
    """Fasst Alternativ-Suchen (``a | b | c``) zu möglichst wenigen eBay-Anfragen zusammen.

    Warum: Jede Alternative war eine eigene Anfrage -- mit allen Suchen ~20 alle
    10 Minuten. Genau diese Menge hat Akamai am 27./28.09. zu 39 Sperren am Tag
    getrieben; dagegen hilft auch kein getarnter Browser.

    eBay versteht ``wort (a,b)`` = "wort und (a oder b)", Wörter in beliebiger
    Reihenfolge -- also exakt die alte Bedeutung, wenn sich die Suchen so zerlegen:

    * gemeinsame Wörter + je genau ein anderes Wort: ``pc (gaming,rtx)``
    * gemeinsame Wörter + vollständiges Kreuzprodukt zweier Wortgruppen:
      ``(ddr4,ddr5) (96gb,128gb)``
    * Modellnamen (jede Alternative hat eine freistehende Modellnummer wie
      3090): Phrasen-ODER
      ``("rtx 3090","rx 7900 xtx")`` -- live geprüft, 60/60 passende Treffer.

    Alles andere bleibt bei einer Anfrage je Alternative (sicher, nur teurer).
    """
    queries = [q.strip() for q in queries if q.strip()]
    if len(queries) <= 1:
        return queries
    if any(ch in q for q in queries for ch in '()",'):  # schon eBay-Syntax: nicht anfassen
        return queries
    out: list[str] = []
    for start in range(0, len(queries), MAX_OR_TERMS):
        batch = queries[start:start + MAX_OR_TERMS]
        out.append(_combine(batch) if len(batch) > 1 else batch[0])
    if any(q is None for q in out):
        return queries
    return out  # type: ignore[return-value]


def _combine(batch: list[str]) -> str | None:
    words = [q.lower().split() for q in batch]
    sets = [frozenset(w) for w in words]
    if any(len(ws) != len(set(ws)) for ws in words):  # doppelte Wörter: lieber nicht
        return None
    common = frozenset.intersection(*sets)
    common_ordered = [w for w in words[0] if w in common]
    rest = [s - common for s in sets]
    prefix = " ".join(common_ordered)
    if all(len(r) == 1 for r in rest) and len(set(rest)) == len(rest):
        group = ",".join(next(iter(r)) for r in rest)
        return f"{prefix} ({group})".strip()
    if all(len(r) == 2 for r in rest):
        x, y = sorted(rest[0])
        a = {t for r in rest if y in r for t in r if t != y}
        b = {t for r in rest if x in r for t in r if t != x}
        if not (a & b) and {frozenset((i, j)) for i in a for j in b} == set(rest) and len(rest) == len(a) * len(b):
            ga = ",".join(sorted(a))
            gb = ",".join(sorted(b))
            return f"{prefix} ({ga}) ({gb})".strip()
    if all(_MODEL_NUMBER.search(q) for q in batch):
        return "(" + ",".join(f'"{q.lower()}"' if " " in q else q.lower() for q in batch) + ")"
    return None
