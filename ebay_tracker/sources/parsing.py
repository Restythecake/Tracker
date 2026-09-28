"""Gemeinsame HTML-Werkzeuge der Parser: Scraplings ``Selector`` mit Selbstheilung.

Scrapling merkt sich beim erfolgreichen Parsen einen Fingerabdruck der ersten
Trefferkarte (Tag, Attribute, Pfad, Nachbarn) in ``scrapling.db`` auf dem
Datenvolume. Findet der normale Selektor nach einem Redesign nichts mehr,
sucht ``adaptive=True`` das ähnlichste Element und ``find_similar`` dessen
Geschwister -- die Karten werden also wiedergefunden, auch wenn die Klassen
oder Attribute, an denen der Selektor hing, weg sind.

Ohne ``configure()`` (Tests, CLI) bleibt die Selbstheilung aus und es wird
nichts auf die Platte geschrieben. Ohne scrapling-Installation liefert
``page()`` ``None`` und die Parser nehmen ihren BeautifulSoup-Pfad.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

try:  # pragma: no cover - hängt von der Installation ab
    from scrapling.parser import Selector

    HAS_SELECTOR = True
except Exception:  # ImportError, aber auch eine kaputte lxml
    Selector = None  # type: ignore[assignment,misc]
    HAS_SELECTOR = False

_storage_file: str | None = None
# Wie oft die Selbstheilung zuletzt gegriffen hat (für Log/Status)
ADAPTIVE_HITS: dict[str, int] = {}


def configure(storage_file: str | None) -> None:
    """Pfad der Fingerabdruck-DB setzen (None = Selbstheilung aus)."""
    global _storage_file
    _storage_file = storage_file


def page(html: str, site: str):
    """Selector für ``html`` -- oder None, wenn scrapling fehlt.

    ``site`` trennt die Fingerabdrücke je Marktplatz (Scrapling speichert je
    Domain). Konstant halten, sonst öffnet jede Such-URL eine eigene Verbindung.
    """
    if not HAS_SELECTOR:
        return None
    if _storage_file:
        return Selector(html, url=site, adaptive=True,
                        storage_args={"storage_file": _storage_file, "url": site})
    return Selector(html, url=site)


def cards(root, css: str, identifier: str, *, may_heal: bool = True):
    """Trefferkarten per ``css``; greift der nicht, per gespeichertem Fingerabdruck.

    ``may_heal=False`` für Seiten, die sagen, dass es nichts gibt: dort fände
    die Ähnlichkeitssuche sonst irgendein Element, das der Karte nur ähnelt.
    Rückgabe: (Karten, True wenn die Selbstheilung sie gefunden hat)."""
    found = root.css(css, identifier=identifier, auto_save=bool(_storage_file))
    if found or not _storage_file or not may_heal:
        return list(found), False
    relocated = root.css(css, identifier=identifier, adaptive=True)
    if not relocated:
        return [], False
    first = relocated[0]
    similar = [first, *first.find_similar()]
    ADAPTIVE_HITS[identifier] = ADAPTIVE_HITS.get(identifier, 0) + 1
    log.warning("Selektor '%s' greift nicht mehr – %d Karte(n) per Fingerabdruck wiedergefunden. "
                "Parser bitte an das neue Layout anpassen.", css, len(similar))
    return similar, True


def text(el) -> str:
    """Sichtbarer Text wie BeautifulSoups ``get_text(" ", strip=True)``."""
    return " ".join(el.get_all_text(" ", strip=True).split()) if el is not None else ""
