
# ---------------------------------------------------------------- restock
import pytest

from ebay_tracker.restock import Offer, RestockCheck, check_otto, check_mms, run_checks
from tests.test_plugin import FakeClient, FakeSource


class FakeShopSession:
    """Antwortet mit vorbereiteten Seiten je URL."""

    def __init__(self, pages: dict[str, bytes | str]):
        self.pages = {k: (v.encode() if isinstance(v, str) else v) for k, v in pages.items()}

    def get(self, url, timeout=25, **kw):
        from urllib.parse import urlsplit
        content = self.pages.get(urlsplit(url).path if urlsplit(url).path in self.pages else url,
                                 self.pages.get(url, b"<html></html>"))

        class R:
            def __init__(self, content, u):
                self.content = content
                self.url = url
                self.status_code = 200

            def decode(self, *a, **k):
                return self.content.decode("utf-8", "replace")
        return R(content, url)


MM_IN = f'''<html><script type="application/ld+json">{{"@type":"Product","name":"NINTENDO The Legend of Zelda Edition - Spielekonsole",
"offers":{{"@type":"Offer","price":519.99,"priceCurrency":"EUR","availability":"https://schema.org/InStock"}}}}</script></html>'''.strip()
MM_OUT = MM_IN.replace("InStock", "OutOfStock")
OTTO_IN = f'''<html><script type="application/ld+json">{{"@type":"Product","name":"Nintendo Switch 2 Konsole The Legend of Zelda Edition",
"offers":{{"price":519.99,"availability":"https://schema.org/InStock"}},"url":"/p/x/"}}</script></html>'''.strip()


def test_mms_reads_availability():
    sess = FakeShopSession({"https://mm/x.html": MM_IN})
    o = check_mms(sess, "MediaMarkt", "https://mm/x.html")
    assert o.available is True and o.price == 519.99
    o = check_mms(FakeShopSession({"https://mm/x.html": MM_OUT}), "Saturn", "https://mm/x.html")
    assert o.available is False


def test_otto_picks_console_not_game():
    game = OTTO_IN.replace("Konsole ", "").replace("Edition", "Softwarekarte").replace("/p/x/", "/p/y/")
    sess = FakeShopSession({"https://otto/suche/": OTTO_IN + game})
    o = check_otto(sess, "https://otto/suche/")
    assert o.available is True and o.url.endswith("/p/x/")


def test_run_checks_reports_errors_without_raising():
    class Boom:
        def get(self, *a, **kw):
            raise TimeoutError("down")
    rc = run_checks(session=Boom(), targets=[{"kind": "mm", "shop": "X", "name": "n", "url": "https://x/"}])
    assert "n" in rc.errors


def test_engine_restock_notify_on_change(tmp_path):
    import sys
    from nest_plugin.engine import Engine
    from nest_plugin.nest_client import NotifyResult
    from nest_plugin.store import Store
    from ebay_tracker.sources import MultiSource
    client = FakeClient()
    engine = Engine(Store(":memory:"), client, source_factory=lambda st: MultiSource({}), pause=lambda s: None)
    engine.update_settings({"searches": [], "restock_urls": ['{"kind":"mm","shop":"MediaMarkt","name":"Switch 2 Zelda","url":"https://mm/x.html"}']}, trigger=False)
    page = MM_IN  # InStock
    sess = FakeShopSession({"https://mm/x.html": page})
    engine._check_restock(engine.settings, session=sess)
    assert len(client.sent) == 1
    assert "verfügbar" in client.sent[0]["title"] and "519,99" in client.sent[0]["body"]
    assert client.sent[0]["level"] == "high"  # die Einstellung (default high), nicht fix
    engine._check_restock(engine.settings, session=sess)
    assert len(client.sent) == 1  # kein Wechsel, keine Meldung
    sess2 = FakeShopSession({"https://mm/x.html": MM_OUT})
    engine._check_restock(engine.settings, session=sess2)
    assert len(client.sent) == 1  # wieder weg -> keine Meldung
    engine._check_restock(engine.settings, session=FakeShopSession({"https://mm/x.html": MM_IN}))
    assert len(client.sent) == 2  # zweiter Restock -> wieder Meldung


# Live-Befund 28.09.2026: Otto liefert den ld+json-Preis als String und die
# Suche enthält ein Zelda-T-Shirt mit "switch konsole" im Namen. Folge waren
# 87 Abstürze in _restock_notify (f"{str:,.2f}") pro Tag.
OTTO_SHIRT = '''<script type="application/ld+json">{"@type":"Product",
"name":"style3 T-Shirt Adventure Link game gamer zelda boy switch konsole breath wild",
"offers":{"price":"18.90","availability":"https://schema.org/InStock"},"url":"/p/shirt/"}</script>'''


def test_price_as_string_is_normalized():
    o = Offer(shop="Otto", name="n", url="u", price="519.99", available=True)
    assert o.price == 519.99 and "519,99" in o.price_note
    o.price = "1.234,50"  # nachträglich gesetzt: price_note bleibt robust
    assert o.price_note.startswith("1.234,50 €")
    assert Offer(shop="x", name="n", url="u", price="kaputt", available=None).price_note == "Preis unbekannt"


def test_otto_ignores_merch_and_string_price():
    page = OTTO_SHIRT + OTTO_IN.replace("519.99", '"549.00"')
    o = check_otto(FakeShopSession({"https://otto/suche/": page}), "https://otto/suche/")
    assert o.url.endswith("/p/x/") and o.price == 549.0
    only_shirt = check_otto(FakeShopSession({"https://otto/suche/": OTTO_SHIRT}), "https://otto/suche/")
    assert only_shirt.available is None


def test_engine_restock_unclear_does_not_flap(tmp_path):
    from nest_plugin.engine import Engine
    from nest_plugin.store import Store
    from ebay_tracker.sources import MultiSource
    client = FakeClient()
    engine = Engine(Store(":memory:"), client, source_factory=lambda st: MultiSource({}), pause=lambda s: None)
    engine.update_settings({"searches": [], "restock_urls": ['{"kind":"otto","name":"Otto","url":"https://otto/suche/"}']},
                           trigger=False)
    stock = FakeShopSession({"https://otto/suche/": OTTO_IN.replace("519.99", '"519.99"')})
    unclear = FakeShopSession({"https://otto/suche/": "<html>Bot-Schutz</html>"})
    engine._check_restock(engine.settings, session=stock)
    assert len(client.sent) == 1 and "519,99" in client.sent[0]["body"]
    for _ in range(3):  # None↔verfügbar im Wechsel darf nicht jedes Mal melden
        engine._check_restock(engine.settings, session=unclear)
        engine._check_restock(engine.settings, session=stock)
    assert len(client.sent) == 1
