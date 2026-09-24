from __future__ import annotations

import logging

from ..config import Search, Settings
from ..models import SOURCE_LABELS, Listing
from .base import Source, SourceError
from .ebay_api import EbayApiSource
from .kleinanzeigen import KleinanzeigenSource
from .scraper import EbayScraperSource

log = logging.getLogger(__name__)

__all__ = [
    "Source", "SourceError", "EbayApiSource", "EbayScraperSource", "KleinanzeigenSource",
    "MultiSource", "create_source", "create_ebay_source",
]


def create_ebay_source(settings: Settings) -> Source:
    has_credentials = bool(settings.ebay_client_id and settings.ebay_client_secret)
    if settings.source == "api" or (settings.source == "auto" and has_credentials):
        if not has_credentials:
            raise SourceError("source=api braucht EBAY_CLIENT_ID und EBAY_CLIENT_SECRET")
        log.info("Nutze offizielle eBay Browse API (%s)", settings.marketplace)
        return EbayApiSource(settings)
    log.info("Nutze eBay-Webseiten-Suche (%s) – für mehr Stabilität API-Keys hinterlegen", settings.domain)
    return EbayScraperSource(settings)


class MultiSource:
    """Fragt je Suche die Quellen ab, die sie nennt, und führt die Treffer zusammen.

    Eine ausgefallene Quelle verschluckt nicht die Treffer der anderen: ``search``
    wirft nur, wenn *alle* Quellen gescheitert sind. ``search_detailed`` liefert
    die Fehler je Quelle zusätzlich, damit eine Oberfläche "eBay blockt gerade,
    Kleinanzeigen läuft" sagen kann statt "Suche kaputt".
    """

    def __init__(self, sources: dict[str, Source]):
        self.sources = sources

    def search_detailed(self, search: Search) -> tuple[list[Listing], dict[str, str]]:
        listings: list[Listing] = []
        errors: dict[str, str] = {}
        for name in search.sources:
            source = self.sources.get(name)
            if source is None:
                errors[name] = f"Quelle '{name}' ist nicht eingerichtet"
                continue
            try:
                listings.extend(source.search(search))
            except Exception as exc:  # jede Quelle ist fremder Code gegen eine fremde Webseite
                errors[name] = str(exc) or exc.__class__.__name__
                log.error("[%s] %s fehlgeschlagen: %s", search.name, SOURCE_LABELS.get(name, name), exc)
        return listings, errors

    def search(self, search: Search) -> list[Listing]:
        listings, errors = self.search_detailed(search)
        if errors and len(errors) == len(search.sources):
            raise SourceError("; ".join(f"{SOURCE_LABELS.get(k, k)}: {v}" for k, v in errors.items()))
        return listings


def create_source(settings: Settings) -> MultiSource:
    return MultiSource({
        "ebay": create_ebay_source(settings),
        "kleinanzeigen": KleinanzeigenSource(settings),
    })
