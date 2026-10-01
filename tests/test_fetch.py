"""Abgestufter Abruf (curl_cffi -> Browser), Parser mit Scrapling und Selbstheilung.

Kein Test startet einen Browser: Stufe 2 ist immer ein Fake.
"""
from __future__ import annotations

import re
from dataclasses import asdict
from pathlib import Path

import pytest

from ebay_tracker.config import Search, Settings
from ebay_tracker.sources import MultiSource, parsing
from ebay_tracker.sources import kleinanzeigen, scraper
from ebay_tracker.sources.base import SourceError
from ebay_tracker.sources.fetch import Browser, Fetcher, FetchStats, Page, blocked_reason
from ebay_tracker.sources.kleinanzeigen import KleinanzeigenSource
from ebay_tracker.sources.scraper import EbayScraperSource
from nest_plugin.engine import PluginSettings
from tests.test_sources import NEW_LAYOUT, OLD_LAYOUT

FIX = Path(__file__).parent / "fixtures"
RESULTS = "<html><ul class='srp-results'>" + "x" * 40_000 + "</ul></html>"


class Resp:
    def __init__(self, status=200, text="", url="https://www.ebay.de/sch/i.html"):
        self.status_code, self.content, self.url = status, text.encode(), url


class FakeHttp:
    """requests-artige Session, die der Reihe nach vorbereitete Antworten liefert."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.urls: list[str] = []
        self.headers: dict = {}

    def get(self, url, timeout=None, **kw):
        self.urls.append(url)
        return self.responses.pop(0)


class FakeBrowser:
    def __init__(self, *pages, available=True):
        self.pages = list(pages)
        self.available = available
        self.urls: list[str] = []
        self.cloudflare: list[bool] = []

    def fetch(self, url, solve_cloudflare=False):
        self.urls.append(url)
        self.cloudflare.append(solve_cloudflare)
        page = self.pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return page


def fetcher(mode="auto", http=(), browser=None, stats=None):
    return Fetcher("ebay", "eBay", mode=mode, session=FakeHttp(*http), browser=browser or FakeBrowser(),
                   stats=stats or FetchStats())


# ------------------------------------------------------------------ Sperr-Erkennung
def test_blocked_reason():
    assert blocked_reason(403, "https://x", "") == "HTTP 403"
    assert blocked_reason(429, "https://x", "") == "HTTP 429"
    assert blocked_reason(503, "https://x", "") == "HTTP 503"
    assert blocked_reason(200, "https://www.ebay.de/splashui/captcha?x", "") == "Captcha"
    assert blocked_reason(200, "https://x", "<title>Pardon Our Interruption</title>") == "Sperrseite"
    # Große echte Ergebnisseite, die irgendwo "captcha" im Skript erwähnt: keine Sperre
    assert blocked_reason(200, "https://x", RESULTS + "captcha") is None
    assert blocked_reason(404, "https://x", "nicht gefunden") is None
    assert blocked_reason(403, "https://x", "<title>Just a moment...</title>") == "Cloudflare"


def test_cloudflare_challenge_asks_browser_to_solve_it():
    b = FakeBrowser(Page(200, "u", RESULTS.encode(), via="browser"), Page(200, "u", RESULTS.encode(), via="browser"))
    f = fetcher(http=[Resp(403, "<title>Just a moment...</title>"), Resp(403, "Access Denied")], browser=b)
    f.get("https://www.vinted.de/catalog")
    f.get("https://www.ebay.de/sch/i.html")
    # solve_cloudflare nur bei echter Cloudflare-Seite: sonst kostet es ~5 s je Abruf
    assert b.cloudflare == [True, False]


# ------------------------------------------------------------------ Eskalation
def test_stage1_ok_never_touches_browser():
    stats = FetchStats()
    b = FakeBrowser()
    f = fetcher(http=[Resp(200, RESULTS)], browser=b, stats=stats)
    page = f.get("https://www.ebay.de/sch/i.html", params={"_nkw": "rtx 3080"})
    assert page.via == "http" and b.urls == []
    assert f.session.urls == ["https://www.ebay.de/sch/i.html?_nkw=rtx+3080"]
    s = stats.snapshot()["ebay"]
    assert (s.requests, s.http_ok, s.rescued, s.blocked) == (1, 1, 0, 0)


def test_blocked_stage1_is_rescued_by_browser_without_pause():
    stats = FetchStats()
    b = FakeBrowser(Page(200, "https://www.ebay.de/sch/i.html", RESULTS.encode(), via="browser"))
    f = fetcher(http=[Resp(403, "Access Denied")], browser=b, stats=stats)
    page = f.get("https://www.ebay.de/sch/i.html")
    assert page.via == "browser" and b.urls == ["https://www.ebay.de/sch/i.html"]
    s = stats.snapshot()["ebay"]
    assert (s.requests, s.http_ok, s.rescued, s.blocked) == (1, 0, 1, 0) and s.last_rescue


def test_both_stages_blocked_raises_blocked():
    stats = FetchStats()
    b = FakeBrowser(Page(403, "https://www.ebay.de/", b"Access Denied", via="browser"))
    f = fetcher(http=[Resp(200, "", url="https://www.ebay.de/splashui/captcha?ap=1")], browser=b, stats=stats)
    with pytest.raises(SourceError) as exc:
        f.get("https://www.ebay.de/sch/i.html")
    assert exc.value.blocked and "Captcha" in str(exc.value) and "Browser: HTTP 403" in str(exc.value)
    assert stats.snapshot()["ebay"].blocked == 1


def test_browser_crash_after_block_still_pauses():
    stats = FetchStats()
    f = fetcher(http=[Resp(403)], browser=FakeBrowser(RuntimeError("Target closed")), stats=stats)
    with pytest.raises(SourceError) as exc:
        f.get("https://www.ebay.de/sch/i.html")
    # eBay sperrt Stufe 1 weiterhin -- sofort wieder anzuklopfen verlängert die Sperre nur
    assert exc.value.blocked and "HTTP 403" in str(exc.value) and "Browser-Abruf fehlgeschlagen" in str(exc.value)
    assert stats.snapshot()["ebay"].blocked == 1


def test_browser_crash_in_stealth_mode_is_no_block():
    f = fetcher("stealth", browser=FakeBrowser(RuntimeError("Target closed")))
    with pytest.raises(SourceError) as exc:
        f.get("https://www.ebay.de/sch/i.html")
    assert not exc.value.blocked


def test_mode_http_and_missing_scrapling_behave_like_before():
    for f in (fetcher("http", http=[Resp(403)]),
              fetcher("auto", http=[Resp(403)], browser=FakeBrowser(available=False))):
        with pytest.raises(SourceError) as exc:
            f.get("https://www.ebay.de/sch/i.html")
        assert exc.value.blocked and f.browser.urls == []


def test_mode_stealth_goes_straight_to_browser():
    stats = FetchStats()
    b = FakeBrowser(Page(200, "u", RESULTS.encode(), via="browser"))
    f = fetcher("stealth", http=[], browser=b, stats=stats)
    assert f.get("https://www.ebay.de/sch/i.html").via == "browser"
    assert f.session.urls == [] and stats.snapshot()["ebay"].browser_ok == 1


def test_non_block_errors_are_left_to_the_source():
    f = fetcher(http=[Resp(404, "weg")])
    assert f.get("https://www.ebay.de/itm/1").status == 404


def test_multisource_pauses_only_when_both_stages_fail():
    """Der Kern der Änderung: eine Browser-Rettung löst keine 30-min-Pause aus."""
    settings = Settings()
    ebay = EbayScraperSource(settings, session=FakeHttp(Resp(403), Resp(403)))
    ebay.fetcher.browser = FakeBrowser(
        Page(200, "u", NEW_LAYOUT.encode(), via="browser"),
        Page(403, "u", b"Access Denied", via="browser"))
    ebay.fetcher.stats = FetchStats()
    multi = MultiSource({"ebay": ebay})
    search = Search(name="t", query="thinkpad", sources=["ebay"])
    listings, errors = multi.search_detailed(search)
    assert [l.item_id for l in listings] == ["226111111111"] and errors == {} and not multi.paused("ebay")
    _, errors = multi.search_detailed(search)
    assert "blockt" in errors["ebay"] and multi.paused("ebay") > 0


# ------------------------------------------------------------------ Browser-Session
class FakeSession:
    instances: list["FakeSession"] = []

    def __init__(self):
        self.fetched: list[str] = []
        self.closed = False
        FakeSession.instances.append(self)

    def start(self):
        pass

    statuses: list[int] = []  # je Nicht-Aufwärm-Abruf; leer = 200

    def fetch(self, url, solve_cloudflare=False, **kw):
        self.fetched.append(url)
        status = 200
        if url.rstrip("/").count("/") > 2 and FakeSession.statuses:
            status = FakeSession.statuses.pop(0)
        return type("R", (), {"status": status, "url": url, "body": b"<html>ok</html>"})()

    def close(self):
        self.closed = True


def test_browser_reuses_one_session_and_warms_up_once_per_host():
    FakeSession.instances = []
    b = Browser(session_factory=FakeSession)
    try:
        assert b.fetch("https://www.ebay.de/sch/i.html?a=1").via == "browser"
        b.fetch("https://www.ebay.de/sch/i.html?a=2")
        b.fetch("https://www.vinted.de/catalog")
    finally:
        b.close()
        b._thread.join(5)
    (s,) = FakeSession.instances
    assert b.starts == 1 and s.closed
    assert s.fetched == ["https://www.ebay.de/", "https://www.ebay.de/sch/i.html?a=1", "https://www.ebay.de/sch/i.html?a=2",
                         "https://www.vinted.de/", "https://www.vinted.de/catalog"]


def test_browser_that_cannot_start_is_skipped_for_a_while():
    def broken():
        raise RuntimeError("Executable doesn't exist")

    b = Browser(session_factory=broken)
    try:
        with pytest.raises(RuntimeError):
            b.fetch("https://www.ebay.de/")
        assert not b.available
    finally:
        b.close()


# ------------------------------------------------------------------ Parser: Scrapling == BeautifulSoup
def _plain(listings):
    return [asdict(l) for l in listings]


@pytest.mark.skipif(not parsing.HAS_SELECTOR, reason="scrapling nicht installiert")
@pytest.mark.parametrize("parse", [
    lambda: kleinanzeigen.parse_results((FIX / "kleinanzeigen_search.html").read_text(encoding="utf-8")),
    lambda: scraper.parse_results(OLD_LAYOUT, "EUR"),
    lambda: scraper.parse_results(NEW_LAYOUT, "EUR"),
], ids=["kleinanzeigen", "ebay-alt", "ebay-neu"])
def test_scrapling_and_bs4_parse_identically(parse, monkeypatch):
    with_scrapling = parse()
    monkeypatch.setattr(parsing, "HAS_SELECTOR", False)  # so läuft das schlanke Image
    without = parse()
    assert with_scrapling and _plain(with_scrapling) == _plain(without)


@pytest.mark.skipif(not parsing.HAS_SELECTOR, reason="scrapling nicht installiert")
def test_adaptive_parser_survives_a_redesign(tmp_path, monkeypatch):
    html = (FIX / "kleinanzeigen_search.html").read_text(encoding="utf-8")
    monkeypatch.setattr(parsing, "ADAPTIVE_HITS", {})
    parsing.configure(str(tmp_path / "scrapling.db"))
    try:
        before = kleinanzeigen.parse_results(html)  # merkt sich den Fingerabdruck
        # Redesign: die Attribute, an denen der Selektor hängt, sind weg
        redesign = re.sub(r"data-adid=", "data-listing=", html).replace("data-href=", "data-link=")
        after = kleinanzeigen.parse_results(redesign)
    finally:
        parsing.configure(None)
    assert [l.item_id for l in after] == [l.item_id for l in before]
    assert [l.price for l in after] == [l.price for l in before]
    assert parsing.ADAPTIVE_HITS == {"kleinanzeigen-card": 1}
    assert kleinanzeigen.parse_results(redesign) == []  # ohne DB keine Selbstheilung


@pytest.mark.skipif(not parsing.HAS_SELECTOR, reason="scrapling nicht installiert")
def test_empty_result_page_does_not_heal_into_garbage(tmp_path, monkeypatch):
    html = (FIX / "kleinanzeigen_search.html").read_text(encoding="utf-8")
    monkeypatch.setattr(parsing, "ADAPTIVE_HITS", {})
    parsing.configure(str(tmp_path / "scrapling.db"))
    try:
        kleinanzeigen.parse_results(html)
        empty = "<p>Es wurden keine Ergebnisse für „xyz“ gefunden.</p>" + re.sub(r"data-adid=", "data-x=", html)
        assert kleinanzeigen.parse_results(empty) == []
    finally:
        parsing.configure(None)
    assert parsing.ADAPTIVE_HITS == {}


def test_kleinanzeigen_empty_search_is_no_layout_alarm(caplog):
    """Live 28.09.: 128x "Seitenlayout geändert" -- die Seite schreibt "keine Ergebnisse" klein."""
    page = "<html><body>" + "x" * 40_000 + "<span>Es wurden keine Ergebnisse für „xyz“ gefunden.</span></body></html>"
    src = KleinanzeigenSource(Settings(), session=FakeHttp(Resp(200, page)))
    src.fetcher.stats = FetchStats()
    assert src.search(Search(name="t", query="xyz", sources=["kleinanzeigen"])) == []
    assert "Seitenlayout" not in caplog.text


# ------------------------------------------------------------------ Einstellungen
def test_fetcher_settings_global_and_per_source():
    s = PluginSettings.from_dict({"fetcher": "http", "fetcher_overrides": ["ebay=stealth", "@vinted = auto",
                                                                           "quatsch=stealth", "ka=turbo"]})
    ts = s.tracker_settings()
    assert ts.fetcher_for("ebay") == "stealth" and ts.fetcher_for("vinted") == "auto"
    assert ts.fetcher_for("kleinanzeigen") == "http" and ts.fetcher_for("mydealz") == "http"
    assert PluginSettings.from_dict({"fetcher": "turbo"}).fetcher == "auto"
    assert EbayScraperSource(Settings(fetcher="http")).fetcher.mode == "http"


# ------------------------------------------------------------------ Anzeige
def test_source_stats_line_in_widget_and_page(monkeypatch):
    from ebay_tracker.sources import fetch
    from nest_plugin import webui, widgets

    stats = FetchStats()
    stats.bump("ebay", requests=17, http_ok=12, rescued=5)
    stats.bump("vinted", requests=2, blocked=2)
    monkeypatch.setattr(widgets, "STATS", stats)
    rows = widgets.source_checks()
    assert rows == [
        {"label": "eBay", "detail": "12 ok · 5 per Browser gerettet · 0 gesperrt", "state": "ok"},
        {"label": "Vinted", "detail": "0 ok · 0 per Browser gerettet · 2 gesperrt", "state": "error"},
    ]
    assert "5 per Browser gerettet" in webui._sources_section()
    assert fetch.STATS is not stats  # globale Statistik unberührt


def test_image_cache_evict_survives_parallel_downloads(tmp_path):
    """Live 28.09.: FileNotFoundError, weil _evict die .tmp-Datei eines laufenden Downloads stat()en wollte."""
    from nest_plugin.server import ImageCache

    cache = ImageCache(tmp_path, max_files=1)
    import os

    for age, name in ((100, "a"), (0, "b")):
        (tmp_path / name).write_bytes(b"x")
        os.utime(tmp_path / name, (1e9 - age, 1e9 - age))
    (tmp_path / "c.123.tmp").write_bytes(b"x")  # Download eines anderen Threads
    cache._evict()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["b", "c.123.tmp"]
    assert cache.get("https://evil.example/x.jpg") is None  # kein offener Proxy


def test_browser_rewarms_once_when_blocked_mid_session():
    """eBay sperrt trotz Cookies: einmal frisch aufwärmen, dann klappt es meist."""
    FakeSession.instances = []
    FakeSession.statuses = [403, 200]
    b = Browser(session_factory=FakeSession)
    try:
        page = b.fetch("https://www.ebay.de/sch/i.html?a=1")
    finally:
        FakeSession.statuses = []
        b.close()
        b._thread.join(5)
    assert page.status == 200
    (s,) = FakeSession.instances
    assert s.fetched == ["https://www.ebay.de/", "https://www.ebay.de/sch/i.html?a=1",
                         "https://www.ebay.de/", "https://www.ebay.de/sch/i.html?a=1"]


def test_after_rescue_auto_goes_straight_to_browser_for_a_while(monkeypatch):
    """Jedes weitere 403 auf Stufe 1 schadet nur dem Ruf der IP: nach einer Rettung direkt Browser."""
    import ebay_tracker.sources.fetch as fetch_mod
    clock = [1000.0]
    monkeypatch.setattr(fetch_mod.time, "monotonic", lambda: clock[0])
    ok = lambda: Page(200, "https://www.ebay.de/sch/i.html", RESULTS.encode(), via="browser")  # noqa: E731
    b = FakeBrowser(ok(), ok())
    f = fetcher(http=[Resp(403, "Access Denied"), Resp(200, RESULTS)], browser=b)
    f.get("https://www.ebay.de/sch/i.html")
    assert f.get("https://www.ebay.de/sch/i.html").via == "browser"  # ohne Stufe 1
    assert len(b.urls) == 2 and len(f.session.responses) == 1
    clock[0] += Fetcher.STICKY_SECONDS + 1
    assert f.get("https://www.ebay.de/sch/i.html").via == "http"  # danach wieder leicht
