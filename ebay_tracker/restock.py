"""Restock-Prüfung bei Händlern: Switch 2 Zelda-Edition (UVP 519,99 €) & Co.

Ein "verfügbar" heißt hier nur: die Seite meldet InStock/Vorbestellbar. Ob der
Einkaufswagen im letzten Schritt wieder "sorry, weg" sagt (genau das, was
Arthur erlebt hat), kann keine Seite garantieren -- deshalb meldet der Tracker
sofort, wenn ein Shop umschaltet, statt den Warenkorb zu testen.

Erkannt werden:

* **MediaMarkt/Saturn** – Produktseite mit ld+json Product, ``offers.availability``
  (InStock/OutOfStock). Die Suchseiten liefern das nicht, also feste Produkt-URLs.
* **Otto** – Suchseite mit ld+json Products inkl. availability. Das Sondermodell
  selbst hängt an einer Variation; der Tracker sucht in der Ergebnisliste nach
  "zelda" + "edition" und nimmt den passenden Treffer.
* **Amazon** – Produktdetailseite, ``id="availability"``-Block.
* **Müller** – ld+json auf der Produktseite (Preis + availability).
* **Nintendo Store** – kein HTML-Shop, sondern Warteschlange (queue-it) plus
  JavaScript-Store; der Tracker erkennt "ausverkauft"-Texte und, ob die
  Warteschlange aktiv ist (dann rollt meist Nachschub an).

Die Shops ändern Layouts gern; fehlt ein Muster, ist das Ergebnis
``available=None`` mit einem Warnhinweis, nie ein falsches "verfügbar".
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from .sources.http import REQUEST_ERRORS, new_session
from .sources.scraper import parse_price

log = logging.getLogger(__name__)

UVP_SWITCH2_ZELDA = 519.99

# Feste Produkt-URLs (Stand 25.09.2026). Bei 404/Umzug: URL in den
# Plugin-Einstellungen austauschen, nicht den Parser verbiegen.
OTTO_CONSOLE_SEARCH = "https://www.otto.de/suche/nintendo-switch-2-zelda-edition/"
MM_CONSOLE = ("https://www.mediamarkt.de/de/product/"
              "_nintendo-switch-2-the-legend-of-zelda-edition-3071423.html")
SATURN_CONSOLE = ("https://www.saturn.de/de/product/"
                  "_nintendo-switch-2-the-legend-of-zelda-edition-3071423.html")
AMAZON_CONSOLE = "https://www.amazon.de/gp/product/B0F2TN43GH"  # gameswirtschaft-Link aufgelöst, 25.09.2026
MUELLER_CONSOLE = "https://www.mueller.de/p/nintendo-switch-2-konsole-legend-of-zelda-PPN3232409/"
NSTORE_CONSOLE = "https://www.nintendo.com/de-de/store/products/"


@dataclass
class Offer:
    shop: str
    name: str
    url: str
    price: float | None
    available: bool | None  # None = unklar, Seite nicht lesbar
    note: str | None = None

    def __post_init__(self) -> None:
        # ld+json liefert den Preis mal als Zahl, mal als String ("18.90", Otto) --
        # alles dahinter rechnet mit float.
        self.price = as_price(self.price)

    @property
    def price_note(self) -> str:
        price = as_price(self.price)  # auch nach nachträglichem Setzen
        if price is None:
            return "Preis unbekannt"
        text = f"{price:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")
        if price <= UVP_SWITCH2_ZELDA:
            return f"{text} (≤ UVP)"
        return f"{text} (+{int(round((price / UVP_SWITCH2_ZELDA - 1) * 100))}% über UVP)"


def as_price(value: Any) -> float | None:
    """519.99 / "519.99" / "519,99" / "1.234,56 €" -> float; Unlesbares -> None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return parse_price(str(value))


@dataclass
class RestockCheck:
    offers: list[Offer] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def available(self) -> list[Offer]:
        return [o for o in self.offers if o.available]

    def summary(self) -> str:
        rows = [f"{'✓' if o.available else '?' if o.available is None else '✗'} {o.shop}: {o.price_note}"
                for o in self.offers]
        for shop, err in self.errors.items():
            rows.append(f"✗ {shop}: {err}")
        return "\n".join(rows) or "Keine Shop-Daten."


def _ld_products(html: str) -> list[dict[str, Any]]:
    """Alle schema.org-Products im HTML, egal wie verschachtelt."""
    out: list[dict[str, Any]] = []
    for m in re.finditer(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        stack = [data]
        while stack:
            x = stack.pop()
            if isinstance(x, dict):
                if x.get("@type") == "Product":
                    out.append(x)
                stack.extend(x.values())
            elif isinstance(x, list):
                stack.extend(x)
    return out


def _first_offer(product: dict[str, Any]) -> dict[str, Any]:
    offers = product.get("offers") or {}
    if isinstance(offers, list):
        return offers[0] if offers else {}
    return offers


def _get(session, url: str) -> str:
    resp = session.get(url, timeout=25)
    return resp.content.decode("utf-8", errors="replace")


def _from_ld_product(shop: str, url: str, html: str, must: list[str]) -> Offer:
    for product in _ld_products(html):
        name = str(product.get("name", ""))
        if not all(k in name.lower() for k in must):
            continue
        offer = _first_offer(product)
        availability = str(offer.get("availability", ""))
        if availability:
            return Offer(shop=shop, name=name, url=url, price=offer.get("price"),
                         available="InStock" in availability)
    return Offer(shop=shop, name=shop, url=url, price=None, available=None,
                 note="Seite gelesen, aber kein Produkt-Muster erkannt")


def check_mms(session, shop: str, url: str | None) -> Offer:
    url = url or (MM_CONSOLE if shop != "Saturn" else SATURN_CONSOLE)
    html = _get(session, url)
    offer = _from_ld_product(shop, url, html, ["zelda"])
    if offer.available is None and "ausverkauft" in html.lower():
        offer.available = False
        offer.note = None
    return offer


def check_otto(session, search_url: str | None = None) -> Offer:
    search_url = search_url or OTTO_CONSOLE_SEARCH
    html = _get(session, search_url)
    best: Offer | None = None
    for product in _ld_products(html):
        name = str(product.get("name", ""))
        low = name.lower()
        # Das Sondermodell: Konsole mit "zelda" im Namen, kein Bundle, kein Spiel.
        # "switch 2" ist Pflicht: am 28.09.2026 matchte sonst ein Zelda-T-Shirt
        # ("… zelda boy switch konsole …", 18,90 €) als "verfügbar".
        if "zelda" not in low or "switch 2" not in low or "konsole" not in low and "edition" not in low:
            continue
        if "softwarekarte" in low or "bundle" in low:
            continue
        offer = _first_offer(product)
        availability = str(offer.get("availability", ""))
        price = as_price(offer.get("price"))
        # Zubehör unter halber UVP ist nie die Konsole
        if not availability or price is not None and price < UVP_SWITCH2_ZELDA / 2:
            continue
        offer_obj = Offer(shop="Otto", name=name, url="https://www.otto.de" + str(product.get("url", "")),
                          price=price, available="InStock" in availability)
        if best is None or offer_obj.available:
            best = offer_obj
    return best or Offer(shop="Otto", name="Sondermodell in der Suche nicht gelistet", url=search_url,
                         price=None, available=None,
                         note="Otto listet die Edition gerade nicht – Shop-Link prüfen")


def check_amazon(session, url: str | None) -> Offer:
    url = url or AMAZON_CONSOLE
    url = url if "th=1" in url else url + ("&th=1" if "?" in url else "?th=1")
    html = _get(session, url)
    if "captcha" in html[:4000].lower() or "Enter the characters you see" in html:
        return Offer(shop="Amazon", name="Amazon", url=url, price=None, available=None,
                     note="Captcha – nächster Lauf versucht es erneut")
    available = None
    if "outOfStockBuyBox" in html or 'id="outOfStock' in html:
        available = False
    elif "submit.addToCart" in html or 'id="add-to-cart' in html or "buyNow" in html:
        available = True
    price = None
    # Preis nur lesen, solange die Buybox aktiv ist: die Ausverkauft-Seite
    # enthält sonst Preise von Zubehör-Empfehlungen (gemessen: 8,05 €).
    if available:
        m = re.search(r'class="a-offscreen">\s*EUR\s*([\d.,]+)', html) or \
            re.search(r'class="a-offscreen">\s*([\d.,]+)\s*€', html) or \
            re.search(r'"displayPrice"\s*:\s*"\(?([\d.,]+)', html)
        if m:
            try:
                price = float(m.group(1).replace(".", "").replace(",", "."))
            except ValueError:
                pass
    return Offer(shop="Amazon", name="Amazon-Angebot", url=url, price=price, available=available,
                 note=None if available is not None else "Buybox nicht erkennbar")


def check_mueller(session, url: str | None) -> Offer:
    url = url or MUELLER_CONSOLE
    html = _get(session, url)
    return _from_ld_product("Müller", url, html, ["zelda"])


def check_nintendo_store(session, url: str | None = None) -> Offer:
    url = url or NSTORE_CONSOLE
    html = _get(session, url)
    if "queue-it" in html[:8000]:
        return Offer(shop="Nintendo Store", name="Warteschlange aktiv", url=url,
                     price=UVP_SWITCH2_ZELDA, available=True,
                     note="Warteschlange läuft – jetzt im Browser einreihen")
    text = re.sub(r"<[^>]+>", " ", html).lower()
    if "ausverkauft" in text or "currently unavailable" in text or "not available" in text:
        return Offer(shop="Nintendo Store", name="Nintendo Store", url=url, price=None, available=False)
    if "vorbestellen" in text or "in den warenkorb" in text or "add to cart" in text:
        return Offer(shop="Nintendo Store", name="Nintendo Store", url=url,
                     price=UVP_SWITCH2_ZELDA, available=True)
    return Offer(shop="Nintendo Store", name="Nintendo Store", url=url, price=None, available=None,
                 note="Status unklar – Seite prüfen")


def run_checks(session=None, targets: list[dict[str, str]] | None = None) -> RestockCheck:
    session = session or new_session("de-DE,de;q=0.9")
    check = RestockCheck()
    jobs = targets or [
        {"kind": "otto", "name": "Switch 2 Zelda Edition (Otto)", "url": OTTO_CONSOLE_SEARCH},
        {"kind": "mm", "shop": "MediaMarkt", "name": "Switch 2 Zelda Edition (MediaMarkt)", "url": MM_CONSOLE},
        {"kind": "mm", "shop": "Saturn", "name": "Switch 2 Zelda Edition (Saturn)", "url": SATURN_CONSOLE},
        {"kind": "amazon", "name": "Switch 2 Zelda Edition (Amazon)", "url": AMAZON_CONSOLE},
        {"kind": "mueller", "name": "Switch 2 Zelda Edition (Müller)", "url": MUELLER_CONSOLE},
    ]
    for job in jobs:
        kind, name, url = job["kind"], job.get("name", job["kind"]), job.get("url")
        try:
            if kind == "otto":
                offer = check_otto(session, url)
            elif kind == "mm":
                offer = check_mms(session, job.get("shop", "MediaMarkt"), url)
            elif kind == "amazon":
                offer = check_amazon(session, url)
            elif kind == "mueller":
                offer = check_mueller(session, url)
            elif kind == "nstore":
                offer = check_nintendo_store(session, url)
            else:
                offer = Offer(shop=kind, name=name, url=url, price=None, available=None,
                              note="unbekannte Prüfart")
            check.offers.append(offer)
        except REQUEST_ERRORS as exc:
            check.errors[name] = f"{exc.__class__.__name__}"
            log.warning("Restock-Check %s fehlgeschlagen: %s", name, exc)
        except (TimeoutError, OSError) as exc:
            check.errors[name] = f"{exc.__class__.__name__}: {exc}"
            log.warning("Restock-Check %s fehlgeschlagen: %s", name, exc)
    return check