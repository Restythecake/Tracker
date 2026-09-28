"""Tests für das Nest-Plugin: Such-Syntax, Engine, Payloads, HTTP-Vertrag."""
from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path

import pytest

from ebay_tracker.config import Search, Settings
from ebay_tracker.models import Listing
from ebay_tracker.sources import MultiSource
from ebay_tracker.sources.base import SourceError
from ebay_tracker.sources.kleinanzeigen import build_url, parse_results
from nest_plugin.dsl import parse_line, parse_lines
from nest_plugin.engine import Engine, PluginSettings
from nest_plugin.manifest import MANIFEST
from nest_plugin.nest_client import NotifyResult
from nest_plugin.store import Store
from nest_plugin.widgets import WIDGETS

FIXTURES = Path(__file__).parent / "fixtures"
PAYLOAD_KINDS = {"stat", "list", "table", "chart", "progress", "status", "cards"}
STATES = {"ok", "warn", "empty", "error", "loading"}


# ------------------------------------------------------------------ dsl
def test_dsl_full_line():
    p = parse_line('name:"Gaming-PC" gaming pc rtx 4070 500-1100€ +"rtx 4070" ~ryzen ~i7 -defekt @ka plz:10115 umkreis:30')
    s = p.search
    assert p.error is None and not p.warnings
    assert s.name == "Gaming-PC" and s.query == "gaming pc rtx 4070"
    assert (s.min_price, s.max_price) == (500, 1100)
    assert s.include_all == ["rtx 4070"] and s.include_any == ["ryzen", "i7"] and s.exclude == ["defekt"]
    assert s.sources == ["kleinanzeigen"] and s.plz == "10115" and s.radius_km == 30


def test_dsl_defaults_and_comparisons():
    s = parse_line("thinkpad t14 <600 >150 zustand:gebraucht kauf:sofort").search
    assert s.name == "thinkpad t14" and (s.min_price, s.max_price) == (150, 600)
    assert s.condition == "used" and s.buying == "fixed" and s.sources == ["ebay", "kleinanzeigen", "vinted", "mydealz"]


def test_dsl_paused_invalid_and_duplicates():
    assert parse_line("// thinkpad").search.enabled is False
    assert parse_line("max:500").error
    assert parse_line("thinkpad plz:12").warnings
    parsed = parse_lines(["rtx 4070", "rtx 4070 max:900"])
    assert [p.search.name for p in parsed] == ["rtx 4070", "rtx 4070 (2)"]


def test_dsl_swaps_inverted_range():
    s = parse_line("pc min:900 max:500").search
    assert (s.min_price, s.max_price) == (500, 900)


# ------------------------------------------------------------ kleinanzeigen
def test_kleinanzeigen_parser_on_real_markup():
    listings = parse_results((FIXTURES / "kleinanzeigen_search.html").read_text(encoding="utf-8"))
    assert [l.item_id for l in listings] == ["3503100782", "3469884724", "3522260098"]
    first, _, vb = listings
    assert first.price == 464 and first.location == "10115 Mitte" and first.source == "kleinanzeigen"
    assert first.url.startswith("https://www.kleinanzeigen.de/s-anzeige/")
    assert first.image_url.startswith("https://img.kleinanzeigen.de/")
    assert vb.price == 395 and vb.price_note == "VB" and vb.price_text == "395 € VB"


def test_kleinanzeigen_urls():
    s = Search(name="x", query="ThinkPad T14", min_price=150, max_price=600)
    assert build_url(s) == ("https://www.kleinanzeigen.de/s-anzeige:angebote/sortierung:neuste/"
                            "preis:150:600/thinkpad-t14/k0")
    assert build_url(Search(name="x", query="pc", max_price=600), "9668", "10115", 40).endswith(
        "/s-10115/anzeige:angebote/sortierung:neuste/preis::600/pc/k0l9668r50")


# ------------------------------------------------------------------ engine
class FakeSource:
    def __init__(self):
        self.items: list[Listing] = []
        self.fail: str | None = None

    def search(self, search):
        if self.fail:
            raise SourceError(self.fail)
        return list(reversed(self.items))  # Marktplätze liefern neueste zuerst


class FakeClient:
    configured = True

    def __init__(self):
        self.sent: list[dict] = []

    def notify(self, title, body="", level="default", url=None):
        self.sent.append({"title": title, "body": body, "level": level, "url": url})
        return NotifyResult(ok=True)

    def self_view(self):
        return {}


def item(source: str, n: int, price: float = 400, title: str = "ThinkPad T14 32GB") -> Listing:
    return Listing(item_id=str(n), title=f"{title} #{n}", price=price, currency="EUR",
                   url=f"https://www.{'ebay.de/itm' if source == 'ebay' else 'kleinanzeigen.de/s-anzeige/x'}/{n}",
                   source=source)


@pytest.fixture
def rig():
    ebay, ka = FakeSource(), FakeSource()
    client = FakeClient()
    engine = Engine(Store(":memory:"), client, source_factory=lambda st: MultiSource({"ebay": ebay, "kleinanzeigen": ka}),
                    pause=lambda s: None)
    engine.update_settings({"searches": ["thinkpad t14 max:600 -defekt"], "max_notifications": 3}, trigger=False)
    return engine, ebay, ka, client


def test_first_run_is_silent_then_new_items_notify(rig):
    engine, ebay, ka, client = rig
    ebay.items = [item("ebay", 1), item("ebay", 2, price=900), item("ebay", 3, title="defekt")]
    ka.items = [item("kleinanzeigen", 10)]
    engine.run_once()
    assert client.sent == []
    assert engine.store.count_tracked() == 2  # 900 € und "defekt" passen nicht

    ebay.items.append(item("ebay", 4, price=350))
    s = engine.run_once()
    assert [l.item_id for _, l in s.new] == ["4"]
    assert client.sent[0]["title"].startswith("350 €") and client.sent[0]["url"].endswith("/4")
    assert client.sent[0]["level"] == "high"


def test_source_down_on_first_run_does_not_flood_later(rig):
    """Der Fehler, den der erste Live-Lauf gezeigt hat: eBay 403 beim ersten Lauf,
    Kleinanzeigen ok -- der erste erfolgreiche eBay-Lauf danach darf nichts melden."""
    engine, ebay, ka, client = rig
    ebay.fail = "403"
    ka.items = [item("kleinanzeigen", 10)]
    engine.run_once()
    ebay.fail = None
    ebay.items = [item("ebay", n) for n in range(1, 30)]
    engine.run_once()
    assert client.sent == []
    ebay.items.append(item("ebay", 99))
    engine.run_once()
    assert len(client.sent) == 1


def test_all_sources_failing_is_an_error_not_an_init(rig):
    engine, ebay, ka, client = rig
    ebay.fail = ka.fail = "down"
    s = engine.run_once()
    assert "thinkpad t14" in s.errors
    ebay.fail = ka.fail = None
    ebay.items = [item("ebay", 1)]
    engine.run_once()
    assert client.sent == []  # erst jetzt still eingelesen


def test_many_new_items_are_bundled(rig):
    engine, ebay, ka, client = rig
    engine.run_once()
    ebay.items = [item("ebay", n) for n in range(1, 8)]
    engine.run_once()
    assert len(client.sent) == 3  # 2 einzeln + 1 Sammelmeldung (max_notifications=3)
    assert client.sent[-1]["title"] == "5 weitere neue Treffer"


def test_changed_search_reinitialises_silently(rig):
    engine, ebay, ka, client = rig
    ebay.items = [item("ebay", 1), item("ebay", 2, price=700)]
    engine.run_once()
    engine.update_settings({"searches": ["thinkpad t14 max:800 -defekt"]}, trigger=False)
    engine.run_once()
    assert client.sent == []  # die 700-€-Anzeige passt jetzt, ist aber nicht neu


def test_masked_secret_does_not_overwrite(rig):
    engine, *_ = rig
    engine.update_settings({"searches": ["x"], "ebay_client_secret": "geheim"}, trigger=False)
    engine.update_settings({"searches": ["x"], "ebay_client_secret": "••••••••"}, trigger=False)
    assert engine.settings.ebay_client_secret == "geheim"


def test_settings_are_tolerant():
    s = PluginSettings.from_dict({"interval_minutes": 1, "notify_level": "loud", "default_sources": ["x"]})
    assert s.interval_minutes == 10 and s.notify_level == "high" and s.default_sources == ["ebay", "kleinanzeigen"]


# ---------------------------------------------------------------- payloads
def _check_payload(p: dict):
    assert p["kind"] in PAYLOAD_KINDS
    state = p.get("state", "ok")
    assert state in STATES
    if state == "empty":
        assert p.get("message")
    if p["kind"] == "list" and state in ("ok", "warn"):
        assert p["items"] and all(i.get("id") and i.get("title") for i in p["items"])
    if p["kind"] == "status" and state in ("ok", "warn"):
        assert p["label"] and all(c.get("label") and c.get("state", "ok") in STATES for c in p["checks"])
    if p["kind"] == "cards" and state in ("ok", "warn"):
        import re
        for i in p["items"]:
            assert i.get("id") and i.get("title")
            assert i.get("url", "https://").startswith("https://")
            if "image" in i:  # Nest: Pfad im Plugin, nur [A-Za-z0-9/._-~+=], nie eine URL
                assert re.fullmatch(r"/[A-Za-z0-9/._~+=-]+", i["image"]), i["image"]
    if p["kind"] in ("list",) and state in ("ok", "warn"):
        assert all(i.get("url", "https://").startswith("https://") for i in p["items"])
    if p["kind"] == "stat" and state in ("ok", "warn"):
        assert p["label"] and isinstance(p["value"], (int, float))
    if p["kind"] == "table" and state in ("ok", "warn"):
        keys = {c["key"] for c in p["columns"]}
        assert all(set(r) >= keys and None not in r.values() for r in p["rows"])
    json.dumps(p)


def test_every_widget_payload_is_valid_before_and_after_a_run(rig):
    engine, ebay, ka, client = rig
    for build in WIDGETS.values():
        _check_payload(build(engine, "3x2"))
    ebay.items = [item("ebay", 1)]
    engine.run_once()
    ebay.items.append(item("ebay", 2))
    engine.run_once()
    for wid, build in WIDGETS.items():
        for size in ("1x1", "2x1", "2x2", "3x2", "4x2", "6x2"):
            _check_payload(build(engine, size))


def test_manifest_matches_widgets_and_contract():
    assert MANIFEST["apiVersion"] == "nest.plugin/v1"
    ids = {w["id"] for w in MANIFEST["widgets"]}
    assert ids == set(WIDGETS) | {"gallery"}  # gallery ist iframe, hat keinen Payload-Builder
    for w in MANIFEST["widgets"]:
        assert w.get("defaultSize") in w["sizes"]
    assert "widgets:iframe" in MANIFEST["scopes"]
    assert set(MANIFEST["settings"]["properties"]["searches"]) >= {"type", "items"}


# -------------------------------------------------------------------- http
def test_http_contract(rig, tmp_path):
    from nest_plugin.server import serve

    engine, ebay, *_ = rig
    ebay.items = [item("ebay", 1)]
    engine.run_once()
    httpd = serve(engine, 0, tmp_path)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def get(path):
        with urllib.request.urlopen(base + path) as r:
            return r.status, r.read().decode()

    def post(path, doc):
        req = urllib.request.Request(base + path, data=json.dumps(doc).encode(), method="POST")
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())

    try:
        assert json.loads(get("/nest/v1/manifest")[1])["id"] == "arthur.tracker"
        assert json.loads(get("/nest/v1/health")[1]) == {"status": "ok"}
        assert get("/icons/icons/tracker.svg")[1].startswith("<svg")
        _check_payload(post("/nest/v1/widgets/latest/data", {"size": "3x2"}))
        assert post("/nest/v1/settings/changed", {"settings": {"searches": ["neu"]}}) == {"ok": True}
        assert [s.query for s in engine.searches] == ["neu"]
        assert post("/nest/v1/actions/test-notify", {})["ok"] is True
        status, html = get("/")
        assert status == 200 and "Tracker" in html
        status, html = get("/go/ebay%3A1")
        assert "https://www.ebay.de/itm/1" in html
        assert "Angebot nicht gefunden" in get("/go/nope")[1]
    finally:
        httpd.shutdown()


# ------------------------------------------------------------ pc evaluation
class DetailSource(FakeSource):
    def __init__(self):
        super().__init__()
        self.details: dict[str, dict] = {}
        self.loaded: list[str] = []

    def load_details(self, listing):
        self.loaded.append(listing.item_id)
        for k, v in self.details.get(listing.item_id, {}).items():
            setattr(listing, k, v)
        listing.details_loaded = True  # wie parse_details im echten Quellcode


def pc(source, n, title, price):
    return Listing(item_id=str(n), title=title, price=price, currency="EUR",
                   url=f"https://www.kleinanzeigen.de/s-anzeige/x/{n}", source=source)


def test_pc_search_notifies_only_real_deals():
    ka = DetailSource()
    client = FakeClient()
    engine = Engine(Store(":memory:"), client, source_factory=lambda st: MultiSource({"kleinanzeigen": ka}),
                    pause=lambda s: None)
    engine.update_settings({"searches": ["gaming pc 400-1000 bewertung:pc @ka"]}, trigger=False)
    engine.run_once()  # stiller erster Lauf
    ka.items = [
        pc("kleinanzeigen", 1, "Gaming PC Ryzen 7 5700X, RTX 4070, 32GB RAM", 850),     # Deal
        pc("kleinanzeigen", 2, "Gaming PC GTX 1650 i5 16GB", 500),                       # zu schwach
        pc("kleinanzeigen", 3, "Gaming PC RTX 4070 Ryzen 5 5600 16GB", 999),             # passt, zu teuer
        pc("kleinanzeigen", 4, "Gaming PC RTX 4070 i7-12700F 32GB", 870),                # laut Details neu
    ]
    ka.details = {"4": {"is_new": True, "description": "Zustand Neu, 0% Finanzierung"}}
    engine.run_once()
    assert [c["url"].rsplit("/", 1)[1] for c in client.sent] == ["1"]
    assert client.sent[0]["title"].startswith("Guter Preis: 850 € · RTX 4070")
    assert "✓ 32 GB RAM" in client.sent[0]["body"]
    shown = {m.listing.item_id: m.listing.assessment["verdict"]
             for m in engine.store.recent_matches(include_silent=True)}
    assert shown == {"1": "gut", "3": "teuer"}  # 2 nie aufgenommen, 4 versteckt
    engine.run_once()
    assert sorted(ka.loaded) == ["1", "3", "4"]  # Details je Anzeige nur einmal


def test_changed_search_drops_old_matches(rig):
    """Neuer Umkreis/Preis: die alten Treffer (anderes Gebiet) gehören nicht mehr zur Suche."""
    engine, ebay, ka, client = rig
    ebay.items = [item("ebay", 1)]
    engine.run_once()
    old = list(engine.store.recent_matches(limit=5, include_silent=True))
    assert len(old) == 1
    ebay.items = [item("ebay", 2)]
    engine.update_settings({"searches": ["thinkpad t14 max:900 -defekt"]}, trigger=False)
    engine.run_once()
    shown = list(engine.store.recent_matches(limit=5, include_silent=True))
    assert [m.listing.item_id for m in shown] == ["2"], "alte Treffer (max:600) müssen weg"


def test_adding_a_source_keeps_history_and_reads_new_source_silently(rig):
    """Seit 0.7: @mydealz dazunehmen löscht keine Treffer, und die neue Quelle meldet
    beim ersten Mal nichts, was dort schon steht."""
    engine, ebay, ka, client = rig
    engine.update_settings({"searches": ["thinkpad t14 max:600 -defekt @ebay"]}, trigger=False)
    ebay.items = [item("ebay", 1)]
    engine.run_once()
    ka.items = [item("kleinanzeigen", 10), item("kleinanzeigen", 11)]
    engine.update_settings({"searches": ["thinkpad t14 max:600 -defekt @ebay @ka"]}, trigger=False)
    engine.run_once()
    assert client.sent == []
    keys = {m.listing.key for m in engine.store.recent_matches(limit=10, include_silent=True)}
    assert keys == {"ebay:1", "kleinanzeigen:10", "kleinanzeigen:11"}
    ka.items.append(item("kleinanzeigen", 12))
    engine.run_once()
    assert [s["url"].rsplit("/", 1)[1] for s in client.sent] == ["12"]


def test_removing_a_source_drops_only_its_matches_and_readd_is_silent(rig):
    engine, ebay, ka, client = rig
    ebay.items, ka.items = [item("ebay", 1)], [item("kleinanzeigen", 10)]
    engine.run_once()
    engine.update_settings({"searches": ["thinkpad t14 max:600 -defekt @ebay"]}, trigger=False)
    engine.run_once()
    keys = {m.listing.key for m in engine.store.recent_matches(limit=10, include_silent=True)}
    assert keys == {"ebay:1"}
    ka.items.append(item("kleinanzeigen", 11))
    engine.update_settings({"searches": ["thinkpad t14 max:600 -defekt @ebay @ka"]}, trigger=False)
    engine.run_once()
    assert client.sent == [], "wieder dazugenommene Quelle liest still neu ein"


def test_pre_07_signature_is_migrated_not_reset(rig):
    from nest_plugin.engine import signature
    engine, ebay, ka, client = rig
    seen = []  # die Suche, wie die Engine sie prüft (mit Preisdeckel usw.)
    check = engine._check
    engine._check = lambda search, source, settings, summary: (seen.append((search, settings)),
                                                                check(search, source, settings, summary))
    ebay.items = [item("ebay", 1)]
    engine.run_once()
    search, settings = seen[-1]
    legacy = signature(search, settings, with_sources=True)
    engine.store.set_signature(search.name, legacy)  # so liegt es nach dem Update in der DB
    ebay.items.append(item("ebay", 2))
    engine.run_once()
    assert [s["url"].rsplit("/", 1)[1] for s in client.sent] == ["2"]
    assert engine.store.search_state(search.name)["signature"] == signature(search, settings)


def test_stored_matches_are_rechecked_with_current_rules(rig):
    engine, ebay, ka, client = rig
    engine.update_settings({"searches": ["ddr4 64gb bewertung:ram ram:64 @ebay"]}, trigger=False)
    good = Listing(item_id="1", title="Corsair DDR4 64GB (2x32GB) 3200", price=90, currency="EUR",
                   url="https://www.ebay.de/itm/1", source="ebay")
    ebay.items = [good]
    engine.run_once()
    laptop = Listing(item_id="2", title="Vivobook 64GB DDR4", price=90, currency="EUR",
                     url="https://www.ebay.de/itm/2", source="ebay")
    engine.store.upsert_listing(laptop)
    engine.store.add_match(engine.searches[0].name, laptop, silent=0)  # von einer älteren Regel durchgelassen
    engine.store.commit()
    engine.run_once()
    keys = {m.key for m in engine.store.recent_matches(limit=10, include_silent=True)}
    assert keys == {"ebay:1"}
