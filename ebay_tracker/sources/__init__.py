from __future__ import annotations

import logging

from ..config import Settings
from .base import Source, SourceError
from .ebay_api import EbayApiSource
from .scraper import EbayScraperSource

log = logging.getLogger(__name__)

__all__ = ["Source", "SourceError", "EbayApiSource", "EbayScraperSource", "create_source"]


def create_source(settings: Settings) -> Source:
    has_credentials = bool(settings.ebay_client_id and settings.ebay_client_secret)
    if settings.source == "api" or (settings.source == "auto" and has_credentials):
        if not has_credentials:
            raise SourceError("source=api braucht EBAY_CLIENT_ID und EBAY_CLIENT_SECRET")
        log.info("Nutze offizielle eBay Browse API (%s)", settings.marketplace)
        return EbayApiSource(settings)
    log.info("Nutze eBay-Webseiten-Suche (%s) – für mehr Stabilität API-Keys hinterlegen", settings.domain)
    return EbayScraperSource(settings)
