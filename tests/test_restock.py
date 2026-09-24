
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
