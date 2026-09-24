"""Fallback ohne API-Keys: liest die normale eBay-Suchergebnisseite (sortiert nach 'Neu eingestellt').

Funktioniert ohne Anmeldung, ist aber anfälliger: eBay ändert gelegentlich das HTML
und kann bei zu vielen Abfragen ein Captcha zeigen. Für Dauerbetrieb lieber die API nutzen.
"""
from __future__ import annotations

import logging
import re

import requests
from bs4 import BeautifulSoup, Tag

from ..config import Search, Settings
from ..models import Listing
from .base import SourceError

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
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
        )
    return list(listings.values())


def _domain_from_href(href: str) -> str:
    m = re.match(r"https?://([^/]+)", href)
    return m.group(1) if m else "www.ebay.de"


class EbayScraperSource:
    def __init__(self, settings: Settings, session: requests.Session | None = None):
        self.settings = settings
        self.session = session or requests.Session()
        self.session.headers.update(HEADERS)

    def build_params(self, search: Search) -> dict[str, str]:
        params = {
            "_nkw": search.query,
            "_sop": "10",  # Neu eingestellt zuerst
            "_ipg": "60" if self.settings.max_results <= 60 else "120",
            "rt": "nc",
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
        if search.category_id:
            params["_sacat"] = search.category_id
        return params

    def search(self, search: Search) -> list[Listing]:
        url = f"https://{self.settings.domain}/sch/i.html"
        resp = self.session.get(url, params=self.build_params(search), timeout=30)
        if resp.status_code != 200:
            raise SourceError(f"eBay-Suche fehlgeschlagen ({resp.status_code})")
        if "captcha" in resp.url.lower() or "splashui/challenge" in resp.url:
            raise SourceError("eBay zeigt ein Captcha – Intervall erhöhen oder API-Keys nutzen")
        listings = parse_results(resp.text, self.settings.currency)
        if not listings and "srp-results" not in resp.text:
            log.warning("Keine Ergebnisse gefunden – evtl. hat eBay das Seitenlayout geändert")
        return listings[: self.settings.max_results]
