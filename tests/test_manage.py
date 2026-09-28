"""Seite zum Einstellen: eigene Suchen, Preisklassen, Archiv (0.9)."""
from __future__ import annotations

from datetime import timedelta

import pytest

from ebay_tracker.sources import MultiSource
from nest_plugin import frame, manage
from nest_plugin.dsl import parse_line
from nest_plugin.engine import Engine
from nest_plugin.manifest import MANIFEST
from nest_plugin.store import Store, iso, now
from tests.test_plugin import FakeClient, FakeSource, item


@pytest.fixture
def rig():
    ebay = FakeSource()
    client = FakeClient()
    engine = Engine(Store(":memory:"), client, source_factory=lambda st: MultiSource({"ebay": ebay}),
                    pause=lambda s: None)
    engine.update_settings({"searches": ['name:"Thinkpad" thinkpad t14 max:600 @ebay'],
                            "default_sources": ["ebay"]}, trigger=False)
    return engine, ebay, client


def edit(engine, **params):
    return manage.handle(engine, params)


# ------------------------------------------------------------------ Formular -> Zeile
def test_build_line_roundtrip():
    f = {"name": "RTX 4070 günstig", "query": "rtx 4070 | rtx 4070 super", "min": "200", "max": "550,50",
         "must": "12gb", "exclude": "defekt, für bastler", "sources": ["ebay", "kleinanzeigen"],
         "condition": "gebraucht", "evaluate": "gpu", "deal": "85", "plz": "49356", "radius": "80"}
    line = manage.build_line(f)
    s = parse_line(line).search
    assert s is not None and s.name == "RTX 4070 günstig"
    assert s.all_queries == ["rtx 4070", "rtx 4070 super"]
    assert (s.min_price, s.max_price) == (200, 550.5)
    assert s.exclude == ["defekt", "für bastler"] and s.include_all == ["12gb"]
    assert s.sources == ["ebay", "kleinanzeigen"] and s.condition == "used"
    assert s.evaluate == "gpu" and s.min_deal == 0.85 and s.plz == "49356" and s.radius_km == 80
    back = manage.fields_of(line)
    assert back["max"] == "550.5" and back["deal"] == "85" and back["sources"] == ["ebay", "kleinanzeigen"]


@pytest.mark.parametrize("f,msg", [
    ({"query": ""}, "Suchbegriff"),
    ({"query": "x", "min": "500", "max": "100"}, "Mindestpreis"),
    ({"query": "x", "plz": "123"}, "PLZ"),
])
def test_build_line_rejects_nonsense(f, msg):
    with pytest.raises(ValueError, match=msg):
        manage.build_line(f)


# ------------------------------------------------------------------ eigene Suchen
def test_custom_search_lifecycle(rig):
    engine, ebay, _ = rig
    r = edit(engine, op="search-save", fields={"name": "Switch", "query": "switch 2", "max": "400"})
    assert r["ok"], r
    names = [(p.search.name, p.origin) for p in engine.parsed]
    assert names == [("Thinkpad", "nest"), ("Switch", "custom")]
    cid = engine.parsed[1].cid

    # doppelter Name wird abgelehnt
    assert not edit(engine, op="search-save", fields={"name": "thinkpad", "query": "x"})["ok"]
    # bearbeiten behält die id
    assert edit(engine, op="search-save", id=cid, fields={"name": "Switch", "query": "switch 2", "max": "350"})["ok"]
    assert engine.parsed[1].cid == cid and engine.parsed[1].search.max_price == 350

    assert edit(engine, op="search-toggle", name="Switch")["ok"]
    assert not engine.parsed[1].search.enabled
    assert edit(engine, op="search-toggle", name="Switch")["ok"]
    assert engine.parsed[1].search.enabled

    assert edit(engine, op="search-delete", name="Switch")["ok"]
    assert [p.search.name for p in engine.parsed] == ["Thinkpad"]


def test_nest_search_pause_hide_and_takeover(rig):
    engine, _, _ = rig
    assert edit(engine, op="search-toggle", name="Thinkpad")["ok"]
    assert not engine.searches[0].enabled  # pausiert, obwohl die Zeile in Nest aktiv ist

    # übernehmen = als eigene speichern, Nest-Zeile ausgeblendet; gleicher Name erlaubt
    r = edit(engine, op="search-save", replaces="Thinkpad",
             fields={"name": "Thinkpad", "query": "thinkpad t14", "max": "500"})
    assert r["ok"], r
    assert [(p.search.name, p.origin) for p in engine.parsed] == [("Thinkpad", "custom")]
    assert engine.hidden_searches == ["Thinkpad"]

    assert edit(engine, op="search-restore", name="Thinkpad")["ok"]
    assert len(engine.parsed) == 2  # Nest-Zeile wieder da -- Name bekommt Suffix


def test_custom_searches_survive_settings_update(rig):
    engine, _, _ = rig
    edit(engine, op="search-save", fields={"query": "steam deck"})
    engine.update_settings({"searches": ["thinkpad x1"], "default_sources": ["ebay"]}, trigger=False)
    assert [p.origin for p in engine.parsed] == ["nest", "custom"]


def test_frame_action_is_declared_and_safe():
    edit_action = next(a for a in MANIFEST["actions"] if a["id"] == "edit")
    assert edit_action.get("frame") is True and not edit_action.get("danger")


def test_unknown_op_is_refused(rig):
    engine, _, _ = rig
    assert edit(engine, op="rm-rf")["ok"] is False


# ------------------------------------------------------------------ Preisklassen
def test_tiers_by_verdict_and_by_median(rig):
    engine, ebay, _ = rig
    ebay.items = [item("ebay", i, price=p) for i, p in enumerate([100, 300, 310, 320, 500], start=1)]
    engine.run_once()
    matches = engine.recent_matches(limit=50, include_silent=True)
    t = manage.tiers(matches)
    assert t["ebay:1"] == "billig" and t["ebay:5"] == "teuer" and t["ebay:3"] == "mittel"


# ------------------------------------------------------------------ Archiv
def test_manual_archive_hides_and_mutes(rig):
    engine, ebay, client = rig
    ebay.items = [item("ebay", 1, price=300)]
    engine.run_once()  # still eingelesen
    assert edit(engine, op="archive", key="ebay:1")["ok"]
    assert engine.recent_matches(limit=10, include_silent=True) == []
    assert [m.key for m in engine.recent_matches(limit=10, include_silent=True, archived=True)] == ["ebay:1"]
    assert engine.store.count_tracked() == 0

    # Preissturz eines archivierten Angebots meldet nicht
    ebay.items = [item("ebay", 1, price=150)]
    engine.run_once()
    assert client.sent == []

    assert edit(engine, op="unarchive", key="ebay:1")["ok"]
    assert len(engine.recent_matches(limit=10, include_silent=True)) == 1


def test_auto_archive_rules(rig):
    engine, ebay, _ = rig
    ebay.items = [item("ebay", i, price=p) for i, p in enumerate([100, 300, 310, 320, 590], start=1)]
    engine.run_once()
    st = engine.store
    old = iso(now() - timedelta(days=30))
    with st._lock:
        st.conn.execute("UPDATE matches SET first_seen = ? WHERE key = 'ebay:2'", (old,))
        st.conn.execute("UPDATE matches SET first_seen = ? WHERE key = 'ebay:5'", (iso(now() - timedelta(hours=60)),))
        st.conn.commit()
    n = engine.tidy()
    keys = {m.key: m.archived_reason for m in engine.recent_matches(limit=10, include_silent=True, archived=True)}
    assert n == 2 and keys == {"ebay:2": "älter als 14 Tage", "ebay:5": "teuer"}

    # Regeln aus -> nichts mehr automatisch
    assert edit(engine, op="rules", rules={"enabled": False, "max_age_days": 7})["ok"]
    assert manage.rules(st)["max_age_days"] == 7 and engine.tidy() == 0
    assert not edit(engine, op="rules", rules={"max_age_days": 999})["ok"]


def test_gone_listing_is_archived_and_revived(rig):
    engine, ebay, _ = rig
    ebay.items = [item("ebay", 1, price=300), item("ebay", 2, price=310), item("ebay", 3, price=320)]
    engine.run_once()
    st = engine.store
    with st._lock:
        st.conn.execute("UPDATE listings SET last_seen = ? WHERE key = 'ebay:1'", (iso(now() - timedelta(hours=72)),))
        st.conn.commit()
    engine.tidy()
    assert st.archive_info("ebay:1")["reason"] == "nicht mehr online"
    engine.run_once()  # taucht wieder auf
    assert st.archive_info("ebay:1") is None


def test_manual_beats_auto(rig):
    engine, ebay, _ = rig
    ebay.items = [item("ebay", 1, price=300)]
    engine.run_once()
    st = engine.store
    st.archive(["ebay:1"], reason="teuer", auto=True)
    st.archive(["ebay:1"], reason="von Hand", auto=False)
    assert st.is_muted("ebay:1")
    st.archive(["ebay:1"], reason="teuer", auto=True)  # auto überschreibt Hand nicht
    assert st.archive_info("ebay:1")["reason"] == "von Hand"


# ------------------------------------------------------------------ Seiten
@pytest.mark.parametrize("view", ["", "billig", "mittel", "teuer", "suchen", "archiv"])
def test_every_view_renders(rig, view):
    engine, ebay, _ = rig
    ebay.items = [item("ebay", i, price=p) for i, p in enumerate([100, 300, 310, 320, 500], start=1)]
    engine.run_once()
    engine.store.archive(["ebay:3"], reason="von Hand", auto=False)
    html = frame.page(engine, {"layout": "page", "v": view})
    assert "<nav class=nav>" in html and "Tracker" in html
    if view == "suchen":
        assert "data-op=search-save" in html and "Nest-Einstellung" in html
    if view == "archiv":
        assert "data-op=rules" in html and "data-act=unarchive" in html
    if view == "billig":
        assert "data-act=archive-tier" in html and "ebay:1" in html


def test_edit_form_prefills_nest_search_as_takeover(rig):
    engine, _, _ = rig
    html = frame.page(engine, {"v": "suchen", "edit": "Thinkpad"})
    assert "data-replaces='Thinkpad'" in html and "value='600'" in html
