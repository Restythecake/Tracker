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
from .base import SourceError
from .http import HAS_CFFI, REQUEST_ERRORS, new_session

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
_NUMBER = re.compile(r"\d{1,3}(?:[.,  ]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?")
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


def parse_results(html: str, currency: str) -> list[Listing]:
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
        if re.search(r"kostenlos|gratis|free", shipping_text, re.IGNORECASE):
            shipping = 0.0
        else:
            shipping = parse_price(shipping_text)

        card_text = _text(card)
        is_auction = bool(re.search(r"\bGebote?\b|\bbids?\b", card_text, re.IGNORECASE))
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
            self.session = session
            self.session.headers.update(HEADERS)
            self._warmed_up = True
        else:
            self.session = new_session()
            # Mit curl_cffi passt der Fingerprint bereits; der Startseitenbesuch
            # kostet nur eine weitere Anfrage, die selbst 403 bekommen kann.
            self._warmed_up = HAS_CFFI

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
        for i, query in enumerate(search.all_queries):
            if i:
                time.sleep(2.5)
            for listing in self._search_one(search, query):
                results.setdefault(listing.item_id, listing)
        return list(results.values())

    def _get(self, url: str, **kwargs):
        self._warm_up()
        try:
            resp = self.session.get(url, timeout=30, **kwargs)
        except REQUEST_ERRORS as exc:
            raise SourceError(f"eBay nicht erreichbar: {exc.__class__.__name__}") from exc
        if resp.status_code in (403, 429):
            # Neue Session für den nächsten Versuch; jetzt nicht nachhaken.
            self.session = new_session() if not isinstance(self.session, requests.Session) or HAS_CFFI \
                else self.session
            self._warmed_up = HAS_CFFI
            raise SourceError(f"eBay blockt die Anfrage ({resp.status_code})", blocked=True)
        if "captcha" in str(resp.url).lower() or "splashui/challenge" in str(resp.url):
            raise SourceError("eBay zeigt ein Captcha", blocked=True)
        if resp.status_code != 200:
            raise SourceError(f"eBay-Suche fehlgeschlagen ({resp.status_code})")
        return resp

    def _search_one(self, search: Search, query: str) -> list[Listing]:
        resp = self._get(f"https://{self.settings.domain}/sch/i.html", params=self.build_params(search, query))
        html = resp.content.decode("utf-8", errors="replace")
        listings = parse_results(html, self.settings.currency)
        if not listings and "srp-results" not in html:
            log.warning("Keine Ergebnisse gefunden – evtl. hat eBay das Seitenlayout geändert")
        return listings[: self.settings.max_results]

    def load_details(self, listing: Listing) -> None:
        """Artikelmerkmale und Verkäuferbeschreibung (liegt in einem iframe auf itm.ebaydesc.com)."""
        resp = self._get(listing.url)
        desc_url = parse_details(resp.content.decode("utf-8", errors="replace"), listing)
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
