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
import os
import random
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from ebay_tracker.config import MARKETPLACES, Search, Settings
from ebay_tracker.filters import rejection_reason
from ebay_tracker import hardware
from ebay_tracker.fraud import assess_risk
from ebay_tracker.geo import Geocoder, plz_of, trip_cost
from ebay_tracker.hardware import Assessment, assess, assess_gpu, assess_ram, market_note, requirements_from
from ebay_tracker.market import Market
from ebay_tracker.restock import UVP_SWITCH2_ZELDA, run_checks
from ebay_tracker.models import SOURCE_LABELS, Listing
from ebay_tracker.sources import MultiSource, create_source

from .dsl import ParsedSearch, parse_lines
from .nest_client import NestClient, NestError
from .store import Store, iso, now

log = logging.getLogger(__name__)

DEFAULT_SEARCHES = [
    'name:"Gaming-PC bis 1000 €" gaming pc | pc rtx 400-1000 bewertung:pc deal:0.9 '
    'zustand:gebraucht kauf:sofort -defekt -bastler',
    'name:"GPU-Schnäppchen im Paket" pc rtx | pc rx | gaming pc bewertung:pc deal:0.78 '
    'zustand:gebraucht kauf:sofort -defekt -bastler',
    'name:"RAM unter Marktwert" ddr4 | ddr5 bewertung:ram 64gb | 96gb | 128gb -defekt -server',
]
LEVELS = ("min", "low", "default", "high", "urgent")


# URL None = der Checker nutzt seine eingebaute URL
RESTOCK_TARGETS_DEFAULT = [
    {"kind": "otto", "name": "Switch 2 Zelda Edition (Otto)", "url": None},
    {"kind": "mm", "shop": "MediaMarkt", "name": "Switch 2 Zelda Edition (MediaMarkt)", "url": None},
    {"kind": "mm", "shop": "Saturn", "name": "Switch 2 Zelda Edition (Saturn)", "url": None},
    {"kind": "amazon", "name": "Switch 2 Zelda Edition (Amazon)", "url": None},
    {"kind": "mueller", "name": "Switch 2 Zelda Edition (Müller)", "url": None},
]



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
    # Shop-URLs für den Restock-Check (leer = eingebaute Defaults). JSON-Liste.
    restock_urls: list[str] = field(default_factory=list)
    restock_interval_seconds: int = 60
    # Harte Obergrenze für ALLE Suchen (0 = aus). Arthur: "alles über 1000 € ist zu viel".
    max_price_cap: int = 1000
    digest_hour: int = 19  # 0 = aus

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
        pick("restock_interval_seconds", int, lambda v: 30 <= v <= 3600)
        pick("max_price_cap", int, lambda v: 0 <= v <= 100000)
        pick("digest_hour", int, lambda v: 0 <= v <= 23)
        if raw.get("restock_urls") is not None:
            if isinstance(raw["restock_urls"], list):
                s.restock_urls = [str(x) for x in raw["restock_urls"]]
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
        prefix = f"↓ von {int(listing.price_before)} € " if listing.price_before else ""
        return (f"{prefix}{listing.price_text} – {search_name}",
                "\n".join(x for x in (listing.title, meta, listing.url) if x))
    head = " · ".join(x for x in (listing.price_text, a.get("summary")) if x)
    warn = "⚠️ " if a.get("verdict") == "verdaechtig" else ""
    label = a.get("label")
    if listing.price_before and listing.price:
        pct = int(round((1 - listing.price / listing.price_before) * 100))
        label = f"Preis −{pct} %"
    title = f"{warn}{label}: {head}"
    lines = [listing.title]
    if listing.price_before:
        lines.append(f"↓ vorher {int(listing.price_before)} €")
    lines += [f"✓ {p}" for p in a.get("pros", [])[:5]]
    lines += [f"✗ {c}" for c in a.get("cons", [])[:4]]
    lines += [meta, listing.url]
    return title[:256], "\n".join(x for x in lines if x)[:4096]


def _kind_of(url: str) -> str:
    if "otto.de" in url: return "otto"
    if "mediamarkt.de" in url or "saturn.de" in url: return "mm"
    if "amazon.de" in url: return "amazon"
    if "mueller.de" in url: return "mueller"
    if "nintendo" in url: return "nstore"
    return "mm"


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
    gpus: set[str] = field(default_factory=set)


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
        # Offline (Tests): keine Shop-, Markt- oder Geo-Abfragen ins Netz
        self.offline = os.environ.get("TRACKER_OFFLINE") == "1" or os.environ.get("TRACKER_DISABLE_RESTOCK") == "1"
        self.geo = None if self.offline else Geocoder(store.get_meta, store.set_meta)
        self.market = None if self.offline else Market(store.get_meta, store.set_meta)
        self._restock_thread: threading.Thread | None = None
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
            if settings.max_price_cap:
                self.store.hide_over_price(settings.max_price_cap)
            if self.market is not None:
                hardware.GPU_MARKET.clear()
                hardware.GPU_MARKET.update(self.market.overrides())
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
            relevant = {g for g in summary.gpus
                        if hardware.GPUS.get(hardware.market_key(g), ("", 0, 0))[1] >= 80}
            if self.market is not None and relevant:
                try:
                    self.market.refresh_stale(relevant, limit=3,
                                              ebay_blocked=source.paused("ebay") > 0 if source else True)
                except Exception:
                    log.exception("Marktpreise nicht aktualisierbar")
            self._maybe_digest(settings)
            return summary

    DETAIL_BUDGET = 6  # Detailseiten je bewerteter Suche und Lauf
    DROP = 0.92         # ab 8 % billiger gilt ein Preis als gesenkt

    # ------------------------------------------------------------ bewerten
    def _quick(self, search: Search, listing: Listing) -> Assessment | None:
        """Bewertung nur aus Titel/Preis -- entscheidet, ob eine Anzeige überhaupt passt."""
        req = requirements_from(search.min_gpu, search.min_ram)
        if search.evaluate == "ram":
            return assess_ram(listing.title, listing.price, description=listing.description or "",
                              min_gb=search.min_ram or 16)
        if search.evaluate == "gpu":
            return assess_gpu(listing.title, listing.price, description=listing.description or "", req=req,
                              new=listing.is_new)
        if search.evaluate == "pc":
            return assess(listing.title, listing.price, description=listing.description or "", req=req,
                          new=listing.is_new, dealer=listing.dealer)
        return None

    def _full(self, search: Search, listing: Listing, settings: PluginSettings) -> Assessment:
        """Volle Bewertung: plus Marktvergleich, Fahrtkosten, Betrugsrisiko."""
        a = self._quick(search, listing)
        assert a is not None
        if not a.ok:
            return a
        # Abholung: Fahrtkosten einrechnen (nur Kleinanzeigen, dort holt man ab)
        home = (search.plz or settings.kleinanzeigen_plz or "").strip()
        plz = plz_of(listing.location)
        if (listing.source == "kleinanzeigen" and home and plz and listing.price
                and a.ratio is not None and a.ratio < 1.1 and self.geo is not None):
            km = self.geo.road_km(home, plz)
            if km is not None and km > 15:
                cost = trip_cost(km)
                eff = self._quick(search, Listing(**{**listing.__dict__, "price": listing.price + cost}))
                if eff is not None and eff.ok:
                    a = eff
                a.trip_km, a.trip_cost = km, cost
                a.cons.append(f"Abholung ~{km} km, ~{cost} € Fahrt eingerechnet")
            elif km is not None:
                a.trip_km, a.trip_cost = km, 0
                a.pros.append(f"Abholung um die Ecke (~{km} km)")
        if a.specs.gpu and search.evaluate == "pc":
            a.market = market_note(listing.price, self.store.market_prices(a.specs.gpu))
            if a.market:
                (a.pros if " unter " in a.market else a.cons).append(a.market)
        # Betrugssignale
        risk = assess_risk(listing.title, listing.description, price_ratio=a.ratio,
                           seller_since=listing.seller_since)
        a.risk = risk.score
        if risk.score >= 25:
            a.cons.insert(0, f"Risiko {risk.level} ({risk.score}/100): " + "; ".join(risk.reasons[:3]))
        if risk.score >= 50 and a.verdict in ("top", "gut"):
            a.verdict = "verdaechtig"
        return a

    def _merge_known(self, listing: Listing) -> Listing | None:
        """Schon geladene Details (Beschreibung, Account-Alter, Zustand) übernehmen.

        Eine frisch gescrapte Anzeige kennt nur Titel/Preis; ohne das würde jede
        Anzeige bei jedem Lauf ihre Detailseite erneut laden."""
        stored = self.store.listing(listing.key)
        if stored is not None and stored.details_loaded:
            listing.description = listing.description if listing.details_loaded else stored.description
            listing.seller_since = listing.seller_since or stored.seller_since
            listing.dealer = listing.dealer or stored.dealer
            listing.condition = listing.condition or stored.condition
            if listing.is_new is None:
                listing.is_new = stored.is_new
            listing.details_loaded = True
        return stored

    def _check_restock(self, settings: PluginSettings, *, session=None) -> None:
        """Händler abfragen und bei Wechsel auf "verfügbar" sofort melden.

        Steht nie still: ein toter Shop ist eine Logzeile, kein fehlgeschlagener
        Suchlauf. Ohne Session (Unit-Tests) wird übersprungen.
        """
        if session is None and os.environ.get("TRACKER_DISABLE_RESTOCK") == "1":
            return
        try:
            targets = [json.loads(u) if u.strip().startswith("{") else {"kind": _kind_of(u), "name": u, "url": u}
                       for u in settings.restock_urls] or RESTOCK_TARGETS_DEFAULT
        except ValueError:
            targets = RESTOCK_TARGETS_DEFAULT
        try:
            result = run_checks(session=session, targets=targets)
        except Exception:  # der Suchlauf darf am Restock-Check nicht hängen
            log.exception("Restock-Checks fehlgeschlagen")
            return
        for offer in result.offers:
            try:
                changed = self.store.update_restock(
                    offer.shop, url=offer.url, name=offer.name, price=offer.price,
                    available=offer.available, note=offer.note)
            except Exception:
                log.exception("Restock-Speicher %s", offer.shop)
                continue
            if changed:
                if offer.available:
                    self._restock_notify(offer, settings)
                log.info("[Restock] %s: %s", offer.shop,
                         "VERFÜGBAR" if offer.available else ("unverkauft" if offer.available is None else "ausverkauft"))
        for name, err in result.errors.items():
            log.info("[Restock] %s nicht lesbar: %s", name, err)

    def _restock_notify(self, offer, settings: PluginSettings) -> None:
        title = f"🎮 {offer.shop}: {offer.name} verfügbar!"
        body = offer.price_note
        if offer.note:
            body += f"\n{offer.note}"
        body += f"\n{offer.url}"
        res = self.client.notify(title=title[:256], body=body[:4096], level=settings.notify_level, url=offer.url)
        if not res.ok:
            self.nest_status = f"Restock-Meldung fehlgeschlagen: {res.error}"

    def _check(self, search: Search, source: MultiSource, settings: PluginSettings, summary: RunSummary) -> None:
        # Der globale Kleinanzeigen-Umkreis gehört zum Ergebnis: ein neuer Ort
        # bringt lauter "neue" Anzeigen, die nur vorher außer Reichweite waren.
        sig = signature(search, settings)
        initialized = self.store.initialized_sources(search.name, sig)
        row = self.store.search_state(search.name)
        if row is not None and row["signature"] != sig:
            # Geändert (z. B. neuer Umkreis): die alten Treffer passen nicht mehr.
            self.store.drop_matches(search.name)
            initialized = set()
        listings, errors = source.search_detailed(search)
        answered = {name for name in search.sources if name not in errors}
        error = None
        if errors and not answered:
            error = "; ".join(f"{SOURCE_LABELS.get(k, k)}: {v}" for k, v in errors.items())
            summary.errors[search.name] = error

        # 1) grob filtern: Suchbegriffe/Preis und -- mit Bewertung -- Titel-Urteil
        matched: list[Listing] = []
        rejected: list[str] = []
        for listing in reversed(listings):  # älteste zuerst, dann stimmt die Reihenfolge der Meldungen
            if rejection_reason(listing, search) is not None:
                rejected.append(listing.key)
                continue
            cap = settings.max_price_cap
            if cap and listing.price is not None and listing.price > cap:
                rejected.append(listing.key)
                continue
            quick = self._quick(search, listing)
            if quick is not None:
                if quick.specs.gpu and listing.price and search.evaluate == "pc":
                    self.store.observe(listing.key, quick.specs.gpu, listing.price)
                    summary.gpus.add(quick.specs.gpu)
                if quick.specs.gpu and search.evaluate == "gpu":
                    summary.gpus.add(quick.specs.gpu)
                if not quick.ok:
                    rejected.append(listing.key)
                    continue
            matched.append(listing)
        self.store.hide_matches(search.name, rejected)

        # 2) je Treffer: Zustand bestimmen und entscheiden, ob gemeldet wird
        new = quiet = 0
        budget = self.DETAIL_BUDGET
        for listing in matched:
            prev = self.store.match_row(search.name, listing.key)
            stored = self._merge_known(listing)
            prev_price = stored.price if stored is not None else None
            first = not settings.notify_first_run and listing.source not in initialized

            silent = 0
            if search.evaluate:
                if (not first and budget > 0 and not listing.details_loaded
                        and search.evaluate in ("pc", "gpu") and listing.source != "vinted"):
                    budget -= 1
                    self.pause(random.uniform(0.8, 1.8))
                    source.load_details(listing)
                a = self._full(search, listing, settings)
                listing.assessment = a.to_dict()
                if not a.ok:
                    silent = 2  # z. B. "Zustand Neu" laut Detailseite
                elif (search.min_deal is not None and a.ratio is not None
                      and a.ratio > search.min_deal and a.verdict != "verdaechtig"):
                    silent = 1  # deal:-Schwelle nicht erreicht
                elif not a.notify_worthy:
                    silent = 1
            dropped = bool(prev_price and listing.price and listing.price <= prev_price * self.DROP)
            if dropped:
                listing.price_before = prev_price

            if prev is None:
                notify = silent == 0 and not first
                self.store.add_match(search.name, listing, silent=silent, known=first)
            else:
                self.store.add_match(search.name, listing, silent=silent)
                # Neu melden, wenn es gerade erst zum Deal wurde -- oder wenn der
                # Preis eines schon gemeldeten/bekannten Deals deutlich fällt.
                notify = silent == 0 and (prev["notified"] == 0 or dropped)
            if notify:
                new += 1
                summary.new.append((search.name, listing))
            elif prev is None:
                quiet += 1
        self.store.commit()
        prices = [l.price for l in matched if l.price is not None and l.price > 0]
        self.store.record_run(search.name, sig, fetched=len(listings), matched=len(matched), new=new,
                              prices=prices, error=error, source_errors=errors, answered=answered)
        log.info("[%s] %d geholt, %d passend, %d zu melden%s%s", search.name, len(listings), len(matched), new,
                 f", {quiet} still" if quiet else "", f" – Fehler: {errors}" if errors else "")

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

    # ---------------------------------------------------------- Tagesüberblick
    def _maybe_digest(self, settings: PluginSettings, *, force: bool = False) -> bool:
        """Einmal am Tag ab ``digest_hour``: was heute passiert ist, ohne Einzel-Pushes.

        Einzelmeldungen gibt es nur für echte Deals; die "fairen" Angebote und
        Preissenkungen darunter landen hier gesammelt."""
        if not settings.digest_hour and not force:
            return False
        local = datetime.now().astimezone()
        today = local.date().isoformat()
        if not force and (local.hour < settings.digest_hour or self.store.get_meta("digest_date") == today):
            return False
        since = local.replace(hour=0, minute=0, second=0, microsecond=0)
        lines = []
        best: list[tuple[float, str, Listing]] = []
        for s in self.searches:
            ms = self.store.recent_matches(limit=200, search=s.name, include_silent=True, since=since)
            fresh = [m for m in ms if m.notified != 2]  # "bekannt" vom Einlesen zählt nicht
            if not fresh:
                continue
            pushed = sum(1 for m in fresh if m.notified)
            lines.append(f"{s.name}: {len(fresh)} neu, {pushed} gemeldet")
            for m in fresh:
                ratio = (m.listing.assessment or {}).get("ratio")
                if ratio:
                    best.append((ratio, s.name, m.listing))
        restock = [r["shop"] for r in self.store.restock_rows() if r["available"]]
        if restock:
            lines.append("Zelda-Edition verfügbar bei: " + ", ".join(restock))
        self.store.set_meta("digest_date", today)
        if not lines:
            return False
        best.sort(key=lambda x: x[0])
        if best:
            lines.append("")
            lines.append("Beste Preise heute:")
            for ratio, name, l in best[:3]:
                summ = (l.assessment or {}).get("summary") or l.title[:40]
                lines.append(f"• {l.price_text} · {summ} ({int(ratio * 100)} % vom Wert)")
        res = self.client.notify(title="Tracker – Tagesüberblick", body="\n".join(lines)[:4096], level="default")
        return res.ok

    # ---------------------------------------------------------- scheduling
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="tracker-scheduler", daemon=True)
        self._thread.start()
        if not self.offline:
            self._restock_thread = threading.Thread(target=self._restock_loop, name="tracker-restock", daemon=True)
            self._restock_thread.start()

    def _restock_loop(self) -> None:
        """Eigener Takt für Shop-Verfügbarkeit: beim Restock zählen Minuten, nicht zehn davon.

        Fünf leichte Seitenabrufe je Minute sind für die Shops unauffällig;
        die Marktplatz-Suche bleibt bei ihrem eigenen, langsameren Intervall."""
        self._stop.wait(8)
        while not self._stop.is_set():
            try:
                self._check_restock(self.settings)
            except Exception:
                log.exception("Restock-Lauf fehlgeschlagen")
            self._stop.wait(self.settings.restock_interval_seconds * random.uniform(0.85, 1.15))

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
