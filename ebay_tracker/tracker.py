"""Hauptlogik: Suchen abfragen, filtern, neue Treffer melden."""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass

from .config import Config, Search
from .filters import rejection_reason
from .models import Listing
from .notifiers import Notifier, notify_all
from .sources import Source
from .storage import Storage

log = logging.getLogger(__name__)


@dataclass
class CheckResult:
    search: str
    fetched: int = 0
    matched: int = 0
    new: int = 0
    error: str | None = None


class Tracker:
    def __init__(self, config: Config, source: Source, storage: Storage, notifiers: list[Notifier]):
        self.config = config
        self.source = source
        self.storage = storage
        self.notifiers = notifiers

    def check_search(self, search: Search) -> CheckResult:
        result = CheckResult(search=search.name)
        try:
            listings = self.source.search(search)
        except Exception as exc:
            result.error = str(exc)
            log.error("Suche '%s' fehlgeschlagen: %s", search.name, exc)
            return result

        result.fetched = len(listings)
        first_run = not self.storage.is_initialized(search.name)
        silent = first_run and not self.config.settings.notify_on_first_run

        # älteste zuerst melden, damit die Reihenfolge im Handy stimmt
        for listing in reversed(listings):
            reason = rejection_reason(listing, search)
            if reason:
                log.debug("[%s] verworfen (%s): %s", search.name, reason, listing.title)
                continue
            result.matched += 1
            if self.storage.is_seen(search.name, listing.key):
                continue
            result.new += 1
            notified = False
            if not silent:
                notified = notify_all(self.notifiers, search.name, listing)
            self.storage.mark_seen(search.name, listing, notified)
        self.storage.mark_initialized(search.name)

        if silent and result.new:
            log.info(
                "[%s] Erster Lauf: %d bestehende Treffer gespeichert, ab jetzt wird nur Neues gemeldet",
                search.name, result.new,
            )
        else:
            log.info("[%s] %d Angebote geprüft, %d passend, %d neu", search.name, result.fetched, result.matched, result.new)
        return result

    def check_all(self) -> list[CheckResult]:
        results = []
        active = [s for s in self.config.searches if s.enabled]
        for i, search in enumerate(active):
            results.append(self.check_search(search))
            if i < len(active) - 1:
                time.sleep(random.uniform(2, 5))  # eBay nicht mit Anfragen fluten
        return results

    def run_forever(self) -> None:
        interval = self.config.settings.interval_minutes * 60
        log.info("Tracker läuft – %d Suche(n), alle %.0f Minuten. Strg+C zum Beenden.",
                 sum(s.enabled for s in self.config.searches), interval / 60)
        while True:
            self.check_all()
            # etwas Zufall, damit die Abfragen nicht exakt periodisch sind
            time.sleep(interval * random.uniform(0.9, 1.1))


def dry_run(config: Config, source: Source) -> list[tuple[Search, Listing, str | None]]:
    """Zeigt, was die Suchen gerade finden würden (ohne Speichern/Benachrichtigen)."""
    rows = []
    for search in config.searches:
        if not search.enabled:
            continue
        try:
            listings = source.search(search)
        except Exception as exc:
            log.error("Suche '%s' fehlgeschlagen: %s", search.name, exc)
            continue
        for listing in listings:
            rows.append((search, listing, rejection_reason(listing, search)))
    return rows
