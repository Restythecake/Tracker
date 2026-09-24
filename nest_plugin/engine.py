"""Scheduler und Suchlogik des Plugins.

Die Engine besitzt die Einstellungen, die daraus geparsten Suchen, die Quellen
und den Speicher. Sie läuft in einem eigenen Thread; der HTTP-Server liest nur
aus dem Speicher und stößt höchstens einen Lauf an. Ein Lauf ist durch ein Lock
geschützt, damit "Jetzt prüfen" und der Timer nicht gleichzeitig dieselbe Suche
zweimal melden.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from ebay_tracker.config import MARKETPLACES, Search, Settings
from ebay_tracker.filters import rejection_reason
from ebay_tracker.hardware import Assessment, assess, market_note, requirements_from
from ebay_tracker.models import SOURCE_LABELS, Listing
from ebay_tracker.sources import MultiSource, create_source

from .dsl import ParsedSearch, parse_lines
from .nest_client import NestClient, NestError
from .store import Store, iso, now

log = logging.getLogger(__name__)

DEFAULT_SEARCHES = [
    'name:"Gaming-PC bis 1000 €" gaming pc | pc rtx 400-1000 bewertung:pc zustand:gebraucht kauf:sofort '
    '-defekt -bastler',
]
LEVELS = ("min", "low", "default", "high", "urgent")


@dataclass
class PluginSettings:
    searches: list[str] = field(default_factory=lambda: list(DEFAULT_SEARCHES))
    interval_minutes: int = 10
    default_sources: list[str] = field(default_factory=lambda: ["ebay", "kleinanzeigen"])
    notify_level: str = "high"
    max_notifications: int = 5
    notify_first_run: bool = False
    kleinanzeigen_plz: str = ""
    kleinanzeigen_radius: int = 0
    ebay_marketplace: str = "EBAY_DE"
    ebay_client_id: str = ""
    ebay_client_secret: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "PluginSettings":
        """Tolerant: was fehlt oder falsch getippt ist, fällt auf den Default zurück.

        Nest validiert Typ und Grenzen schon beim Speichern (§9.4); hier geht es
        um Einstellungen, die nie gespeichert wurden (frische Installation) und
        um ``null`` = gelöscht.
        """
        s = cls()
        raw = raw or {}

        def pick(key: str, kind: type, ok: Callable[[Any], bool] = lambda v: True) -> None:
            value = raw.get(key)
            if value is None:
                return
            try:
                value = kind(value) if kind is not list else [str(v) for v in value]
            except (TypeError, ValueError):
                return
            if ok(value):
                setattr(s, key, value)

        pick("searches", list)
        pick("interval_minutes", int, lambda v: 3 <= v <= 1440)
        pick("default_sources", list, lambda v: bool(v) and all(x in SOURCE_LABELS for x in v))
        pick("notify_level", str, lambda v: v in LEVELS)
        pick("max_notifications", int, lambda v: 1 <= v <= 50)
        pick("notify_first_run", bool)
        pick("kleinanzeigen_plz", str)
        pick("kleinanzeigen_radius", int, lambda v: 0 <= v <= 500)
        pick("ebay_marketplace", str, lambda v: v in MARKETPLACES)
        pick("ebay_client_id", str)
        pick("ebay_client_secret", str)
        return s

    def tracker_settings(self) -> Settings:
        plz = self.kleinanzeigen_plz.strip()
        return Settings(
            marketplace=self.ebay_marketplace,
            source="auto",
            interval_minutes=self.interval_minutes,
            max_results=60,
            ebay_client_id=self.ebay_client_id or None,
            ebay_client_secret=self.ebay_client_secret or None,
            kleinanzeigen_plz=plz or None,
            kleinanzeigen_radius_km=self.kleinanzeigen_radius or None,
        )


def format_notification(search_name: str, listing: Listing) -> tuple[str, str]:
    """Titel und Text einer Treffer-Meldung. Mit Bewertung: Specs, Urteil, Pro/Contra."""
    meta = " · ".join(x for x in (listing.source_label, listing.location, listing.shipping_note) if x)
    a = listing.assessment
    if not a:
        return f"{listing.price_text} – {search_name}", "\n".join(x for x in (listing.title, meta, listing.url) if x)
    head = " · ".join(x for x in (listing.price_text, a.get("summary")) if x)
    warn = "⚠️ " if a.get("verdict") == "verdaechtig" else ""
    title = f"{warn}{a.get('label')}: {head}"
    lines = [listing.title]
    lines += [f"✓ {p}" for p in a.get("pros", [])[:5]]
    lines += [f"✗ {c}" for c in a.get("cons", [])[:4]]
    lines += [meta, listing.url]
    return title[:256], "\n".join(x for x in lines if x)[:4096]


def signature(search: Search, settings: "PluginSettings | None" = None) -> str:
    """Was das Ergebnis einer Suche bestimmt. Name und Pause gehören nicht dazu."""
    data = asdict(search)
    if settings is not None and "kleinanzeigen" in search.sources and not search.plz:
        data["_ka_area"] = [settings.kleinanzeigen_plz, settings.kleinanzeigen_radius]
    data.pop("name", None)
    data.pop("enabled", None)
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


@dataclass
class RunSummary:
    started: datetime
    finished: datetime | None = None
    searches: int = 0
    new: list[tuple[str, Listing]] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    notified: int = 0
    notify_error: str | None = None


class Engine:
    def __init__(self, store: Store, client: NestClient, *,
                 source_factory: Callable[[Settings], MultiSource] = create_source,
                 pause: Callable[[float], None] = time.sleep):
        self.store = store
        self.client = client
        self.source_factory = source_factory
        self.pause = pause
        self._run_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.settings = PluginSettings.from_dict(store.get_meta("settings"))
        self.settings_known = store.get_meta("settings") is not None
        self.parsed: list[ParsedSearch] = []
        self.source: MultiSource | None = None
        self.last_run: RunSummary | None = None
        self.next_run_at: datetime | None = None
        self.nest_status: str | None = None  # letzter Fehler beim Rückruf zu Nest
        self._apply(self.settings)

    # ---------------------------------------------------------- settings
    def _apply(self, settings: PluginSettings) -> None:
        with self._state_lock:
            old = self.settings
            self.settings = settings
            self.parsed = parse_lines(settings.searches, settings.default_sources)
            if self.source is None or old.tracker_settings() != settings.tracker_settings():
                self.source = self.source_factory(settings.tracker_settings())
            names = {p.search.name for p in self.parsed if p.search}
            self.store.forget_searches_except(names)

    def update_settings(self, raw: dict[str, Any] | None, *, trigger: bool = True) -> bool:
        """Neue Einstellungen übernehmen; True, wenn sich etwas geändert hat."""
        if raw is None:
            return False
        raw = dict(raw)
        # Nest schickt dem Plugin die echten Werte (§9.4). Kommt trotzdem die
        # Browser-Maske an, ist das kein neues Passwort, sondern "unverändert".
        for secret in ("ebay_client_secret",):
            if raw.get(secret) == "••••••••":
                raw[secret] = self.settings.ebay_client_secret
        new = PluginSettings.from_dict(raw)
        with self._state_lock:
            changed = asdict(new) != asdict(self.settings) or not self.settings_known
            self.settings_known = True
            if not changed:
                return False
            self.store.set_meta("settings", raw)
            self._apply(new)
        log.info("Einstellungen übernommen: %d Suche(n), alle %d min",
                 len(self.searches), new.interval_minutes)
        if trigger:
            self._wake.set()  # geänderte Suchen sofort einmal laufen lassen
        return True

    def refresh_from_nest(self) -> None:
        """Beim Start: die echten Einstellungen holen (Passwortfelder unmaskiert, §5.4)."""
        if not self.client.configured:
            self.nest_status = "Kein Plugin-Token – Benachrichtigungen sind aus"
            return
        try:
            view = self.client.self_view()
            self.nest_status = None
            settings = view.get("settings")
            if isinstance(settings, dict):
                self.update_settings(settings, trigger=False)
            granted = set(view.get("grantedScopes") or [])
            if "notify" not in granted:
                self.nest_status = "Scope 'notify' nicht freigegeben – keine Benachrichtigungen"
        except NestError as exc:
            self.nest_status = f"Nest nicht erreichbar ({exc.code})"
            log.warning("Konnte /self nicht lesen: %s", exc)

    @property
    def searches(self) -> list[Search]:
        return [p.search for p in self.parsed if p.search is not None]

    def recent_matches(self, **kw):
        """Nur Treffer von Suchen, die es noch gibt -- gelöschte verschwinden aus der Anzeige."""
        if not kw.get("search"):
            kw["searches"] = [s.name for s in self.searches]
        return self.store.recent_matches(**kw)

    def count_new_since(self, since):
        return self.store.count_new_since(since, [s.name for s in self.searches])

    @property
    def active_searches(self) -> list[Search]:
        return [s for s in self.searches if s.enabled]

    # ---------------------------------------------------------- the run
    def run_once(self) -> RunSummary:
        with self._run_lock:
            summary = RunSummary(started=now())
            self.last_run = summary
            with self._state_lock:
                searches = list(self.active_searches)
                source = self.source
                settings = self.settings
            for i, search in enumerate(searches):
                if i:
                    self.pause(random.uniform(1.5, 3.5))  # Marktplätze nicht fluten
                self._check(search, source, settings, summary)
            summary.searches = len(searches)
            self._notify(summary, settings)
            summary.finished = now()
            self.store.set_meta("last_run", {
                "started": iso(summary.started), "finished": iso(summary.finished),
                "new": len(summary.new), "errors": summary.errors, "notified": summary.notified,
                "notify_error": summary.notify_error,
            })
            if random.random() < 0.05:
                self.store.prune()
            return summary

    DETAIL_BUDGET = 6  # Detailseiten je bewerteter Suche und Lauf

    def _assess(self, search: Search, listing: Listing, *, with_market: bool = False) -> Assessment:
        req = requirements_from(search.min_gpu, search.min_ram)
        a = assess(listing.title, listing.price, description=listing.description or "", req=req,
                   new=listing.is_new, dealer=listing.dealer)
        if with_market and a.specs.gpu:
            a.market = market_note(listing.price, self.store.market_prices(a.specs.gpu))
            if a.market:
                (a.pros if " unter " in a.market else a.cons).append(a.market)
        return a

    def _check(self, search: Search, source: MultiSource, settings: PluginSettings, summary: RunSummary) -> None:
        # Der globale Kleinanzeigen-Umkreis gehört zum Ergebnis: ein neuer Ort
        # bringt lauter "neue" Anzeigen, die nur vorher außer Reichweite waren.
        sig = signature(search, settings)
        initialized = self.store.initialized_sources(search.name, sig)
        listings, errors = source.search_detailed(search)
        answered = {name for name in search.sources if name not in errors}
        error = None
        if errors and not answered:
            error = "; ".join(f"{SOURCE_LABELS.get(k, k)}: {v}" for k, v in errors.items())
            summary.errors[search.name] = error

        matched: list[tuple[Listing, Assessment | None]] = []
        for listing in reversed(listings):  # älteste zuerst, dann stimmt die Reihenfolge der Meldungen
            if rejection_reason(listing, search) is not None:
                continue
            a = None
            if search.evaluate:
                a = self._assess(search, listing)
                if a.specs.gpu and listing.price:
                    self.store.observe(listing.key, a.specs.gpu, listing.price)
                if not a.ok:
                    continue
            matched.append((listing, a))

        new = silent_new = 0
        budget = self.DETAIL_BUDGET
        for listing, a in matched:
            if self.store.is_matched(search.name, listing.key):
                self.store.upsert_listing(listing)  # Preis kann sich geändert haben
                continue
            # Still, solange diese Quelle für diese Suche noch nie eingelesen wurde
            silent: int = int(not settings.notify_first_run and listing.source not in initialized)
            if a is not None and not silent:
                if budget > 0:
                    budget -= 1
                    self.pause(random.uniform(0.8, 1.8))
                    source.load_details(listing)
                a = self._assess(search, listing, with_market=True)
                if not a.ok:
                    silent = 2  # z. B. "Zustand Neu" oder RAM zu klein laut Beschreibung
                elif not a.notify_worthy:
                    silent = 1  # passt, aber kein Deal: in der Galerie, ohne Meldung
            if a is not None:
                listing.assessment = a.to_dict()
            self.store.add_match(search.name, listing, silent=silent)
            if silent:
                silent_new += 1
            else:
                new += 1
                summary.new.append((search.name, listing))
        self.store.commit()
        prices = [l.price for l, _ in matched if l.price is not None and l.price > 0]
        self.store.record_run(search.name, sig, fetched=len(listings), matched=len(matched), new=new,
                              prices=prices, error=error, source_errors=errors, answered=answered)
        log.info("[%s] %d geholt, %d passend, %d neu%s%s", search.name, len(listings), len(matched), new,
                 f", {silent_new} still/aussortiert" if silent_new else "",
                 f" – Fehler: {errors}" if errors else "")

    def _notify(self, summary: RunSummary, settings: PluginSettings) -> None:
        if not summary.new:
            return
        if not self.client.configured:
            summary.notify_error = "kein Token"
            return
        limit = settings.max_notifications
        items = summary.new
        single = items if len(items) <= limit else items[: limit - 1]
        for search_name, listing in single:
            title, body = format_notification(search_name, listing)
            result = self.client.notify(title=title, body=body, level=settings.notify_level, url=listing.url)
            if result.ok:
                summary.notified += 1
                self.store.mark_notified(search_name, listing.key)
            else:
                summary.notify_error = result.error
                break
        rest = items[len(single):]
        if rest and not summary.notify_error:
            by_search: dict[str, int] = {}
            for name, _ in rest:
                by_search[name] = by_search.get(name, 0) + 1
            lines = [f"{n}× {name}" for name, n in by_search.items()]
            result = self.client.notify(
                title=f"{len(rest)} weitere neue Treffer",
                body="\n".join(lines) + "\nAlle im Tracker-Widget in Nest.",
                level=settings.notify_level,
            )
            if result.ok:
                summary.notified += 1
                for name, listing in rest:
                    self.store.mark_notified(name, listing.key)
        self.store.commit()
        self.nest_status = None if not summary.notify_error else f"Benachrichtigung fehlgeschlagen: {summary.notify_error}"

    # ---------------------------------------------------------- scheduling
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="tracker-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def trigger(self) -> None:
        self._wake.set()

    @property
    def running(self) -> bool:
        return self._run_lock.locked()

    def _loop(self) -> None:
        # Kurz warten: beim Containerstart ruft Nest oft gleich die Widgets ab
        # und bringt dabei die Einstellungen mit.
        self._wake.wait(5)
        while not self._stop.is_set():
            self._wake.clear()
            if self.active_searches:
                try:
                    self.run_once()
                except Exception:  # der Scheduler darf nie sterben
                    log.exception("Lauf fehlgeschlagen")
            interval = self.settings.interval_minutes * 60
            delay = interval * random.uniform(0.9, 1.1)
            self.next_run_at = now() + timedelta(seconds=delay)
            self._wake.wait(delay)
