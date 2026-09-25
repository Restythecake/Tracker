"""Vinted, Betrugssignale, Fahrtkosten, Preissenkungen, GPU-Einzelkarten, Tagesüberblick."""
from datetime import date
from pathlib import Path

from ebay_tracker.fraud import assess_risk
from ebay_tracker.geo import Geocoder, trip_cost
from ebay_tracker.hardware import assess_gpu
from ebay_tracker.sources import MultiSource
from ebay_tracker.sources.vinted import parse_results, parse_title
from nest_plugin.engine import Engine
from nest_plugin.store import Store
from tests.test_plugin import FakeClient, FakeSource, item, pc

FIX = Path(__file__).parent / "fixtures"


def test_vinted_parser():
    ls = parse_results((FIX / "vinted_catalog.html").read_text())
    assert len(ls) == 3 and all(l.source == "vinted" and l.url.startswith("https://www.vinted.de/items/") for l in ls)
    assert ls[1].price == 158.2 and ls[1].price_note == "inkl. Käuferschutz" and ls[1].is_new


def test_vinted_title_split():
    assert parse_title("MSI RTX 3090, Marke: MSI, Zustand: Gut, 700.00 €, 736.20 €") == \
        ("MSI RTX 3090", "Gut", 700.0, 736.2)


def test_fraud_new_account_plus_low_price():
    r = assess_risk("RTX 5080 PC", "schreib mir auf WhatsApp", price_ratio=0.55,
                    seller_since="2026-08-31", today=date(2026, 9, 25))
    assert r.level == "hoch" and any("25 Tage" in x for x in r.reasons)
    assert assess_risk("RTX 4070 PC", "Abholung", price_ratio=0.9, seller_since="2014-01-01").score == 0


def test_geo_and_trip_cost():
    coords = {"49356": (52.61, 8.37), "28215": (53.09, 8.80)}
    g = Geocoder({}.get, lambda k, v: None, fetch=lambda plz: coords.get(plz, (0.0, 0.0)))
    km = g.road_km("49356", "28215")
    assert 70 <= km <= 90 and trip_cost(km) == 40
    assert g.road_km("49356", "99999") is None


def test_gpu_single_card():
    a = assess_gpu("ASUS TUF RTX 3090 24GB", 800)
    assert a.ok and a.verdict == "top"
    assert not assess_gpu("Gaming PC RTX 3090 Ryzen 7", 800).ok
    assert not assess_gpu("RTX 3090 Kühlkörper keine Karte", 80).ok


def _engine(sources, searches):
    client = FakeClient()
    e = Engine(Store(":memory:"), client, source_factory=lambda st: MultiSource(sources), pause=lambda s: None)
    e.update_settings({"searches": searches}, trigger=False)
    return e, client


def test_price_drop_on_known_listing_notifies():
    ebay = FakeSource()
    e, client = _engine({"ebay": ebay}, ["thinkpad t14 max:600 @ebay"])
    ebay.items = [item("ebay", 1, price=500)]
    e.run_once()  # still eingelesen
    e.run_once()
    assert client.sent == []
    ebay.items = [item("ebay", 1, price=420)]
    e.run_once()
    assert len(client.sent) == 1 and client.sent[0]["title"].startswith("↓ von 500 €")
    e.run_once()
    assert len(client.sent) == 1  # nicht nochmal


def test_deal_that_appears_later_notifies_once():
    ka = FakeSource()
    e, client = _engine({"kleinanzeigen": ka}, ["gaming pc bewertung:pc deal:0.9 @ka"])
    ka.items = [pc("kleinanzeigen", 1, "Gaming PC Ryzen 7 5700X, RTX 4070, 32GB RAM", 1200)]
    e.run_once()
    e.run_once()
    assert client.sent == []  # zu teuer
    ka.items = [pc("kleinanzeigen", 1, "Gaming PC Ryzen 7 5700X, RTX 4070, 32GB RAM", 760)]
    e.run_once()
    assert len(client.sent) == 1 and "RTX 4070" in client.sent[0]["title"]
    e.run_once()
    assert len(client.sent) == 1


def test_risky_seller_marked_suspicious():
    class Detail(FakeSource):
        def load_details(self, l):
            l.seller_since = date.today().replace(day=1).isoformat()
            l.description = "Nur Versand, bitte per PayPal Freunde"
            l.details_loaded = True
    ka = Detail()
    e, client = _engine({"kleinanzeigen": ka}, ["gaming pc bewertung:pc @ka"])
    e.run_once()
    ka.items = [pc("kleinanzeigen", 7, "Gaming PC Ryzen 7 5700X, RTX 4070, 32GB RAM", 800)]
    e.run_once()
    assert client.sent and client.sent[0]["title"].startswith("⚠️")
    assert "Risiko" in client.sent[0]["body"]


def test_digest_once_per_day():
    ebay = FakeSource()
    e, client = _engine({"ebay": ebay}, ["thinkpad t14 max:600 @ebay"])
    e.run_once()
    ebay.items = [item("ebay", 5, price=450)]
    e.run_once()
    before = len(client.sent)
    assert e._maybe_digest(e.settings, force=True)
    assert client.sent[-1]["title"] == "Tracker – Tagesüberblick" and len(client.sent) == before + 1
    assert not e._maybe_digest(e.settings)  # heute schon gesendet
