"""Nest Plugins v2: Token per Claim, Galerie als natives ``media``-Widget, kein iframe."""
from __future__ import annotations

import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ebay_tracker.models import Listing
from ebay_tracker.sources import MultiSource
from nest_plugin.engine import Engine
from nest_plugin.manifest import MANIFEST
from nest_plugin.nest_client import NestClient, NestError
from nest_plugin.store import Store
from nest_plugin.widgets import WIDGETS, gallery


# ------------------------------------------------------------------ ein kleines Nest
class FakeNest:
    """Nests Plugin-API, so weit der Tracker sie nutzt: /claim, /self, /notify."""

    def __init__(self):
        self.refuse: list[tuple[int, str]] = []  # Antworten auf die nächsten Claims, danach Erfolg
        self.claims = 0
        self.valid = ""  # der eine gültige Token; jeder Claim ersetzt ihn
        self.notified: list[dict] = []
        self.calls: list[str] = []
        nest = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, doc):
                body = json.dumps(doc).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authorized(self):
                return bool(nest.valid) and self.headers.get("Authorization") == f"Bearer {nest.valid}"

            def do_GET(self):
                nest.calls.append("GET " + self.path)
                if not self._authorized():
                    return self._send(401, {"error": {"code": "unauthorized", "message": "bad token"}})
                self._send(200, {"data": {"settings": {"searches": ["aus nest"]}, "grantedScopes": ["notify"]}})

            def do_POST(self):
                nest.calls.append("POST " + self.path)
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.path == "/claim":
                    assert not raw and "Authorization" not in self.headers  # ohne Body, ohne Auth
                    nest.claims += 1
                    if nest.refuse:
                        status, code = nest.refuse.pop(0)
                        return self._send(status, {"error": {"code": code, "message": "nein", "requestId": "r1"}})
                    nest.valid = f"nestp_geheim{nest.claims}"
                    return self._send(200, {"data": {"pluginId": "arthur.tracker", "token": nest.valid,
                                                      "webhookSecret": "whsec_geheim", "grantedScopes": ["notify"]}})
                if not self._authorized():
                    return self._send(401, {"error": {"code": "unauthorized", "message": "bad token"}})
                nest.notified.append(json.loads(raw))
                self._send(200, {"data": {"ok": True}})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"


@pytest.fixture
def nest():
    fake = FakeNest()
    yield fake
    fake.httpd.shutdown()
    fake.httpd.server_close()


# ------------------------------------------------------------------ Claim
def test_claim_fetches_token_and_never_logs_it(nest, caplog, monkeypatch):
    monkeypatch.setenv("NEST_PLUGIN_TOKEN", "nestp_alt_aus_der_umgebung")  # wird ignoriert
    caplog.set_level(logging.DEBUG)
    client = NestClient(nest.url)
    assert not client.configured
    with pytest.raises(NestError) as no_token:
        client.self_view()
    assert no_token.value.code == "no_token" and nest.calls == []
    assert client.claim() is True
    assert client.configured and client.granted_scopes == ["notify"] and client.claim_error is None
    assert client.self_view()["settings"] == {"searches": ["aus nest"]}
    assert client.notify("Titel", "Text").ok
    assert "geheim" not in caplog.text and "geheim" not in repr(vars(client).keys())
    assert not hasattr(client, "webhook_secret")  # nicht gebraucht, also nicht aufbewahrt


@pytest.mark.parametrize("status,code", [(403, "pending"), (403, "not_a_plugin"), (404, "not_found")])
def test_claim_refusal_is_normal(nest, status, code):
    nest.refuse = [(status, code)]
    client = NestClient(nest.url)
    assert client.claim() is False
    assert not client.configured and client.claim_error == code
    assert client.notify("x").ok is False  # läuft weiter, meldet nur nicht
    assert client.claim() is True and client.claim_error is None


def test_claim_survives_unreachable_nest():
    client = NestClient("http://127.0.0.1:9", timeout=1)
    assert client.claim() is False and client.claim_error == "unreachable"


def test_keep_claimed_retries_until_nest_accepts(nest):
    nest.refuse = [(403, "pending"), (404, "not_found")]
    client = NestClient(nest.url)
    stop = threading.Event()
    seen: list[bool] = []

    def after():
        seen.append(client.configured)
        if client.configured:
            stop.set()

    worker = threading.Thread(target=client.keep_claimed, args=(stop, after, 0.01), daemon=True)
    worker.start()
    worker.join(5)
    assert not worker.is_alive()
    assert seen == [False, False, True] and nest.claims == 3


def test_keep_claimed_leaves_a_working_token_alone(nest):
    client = NestClient(nest.url)
    client.claim()
    stop = threading.Event()
    worker = threading.Thread(target=client.keep_claimed, args=(stop, None, 0.01), daemon=True)
    worker.start()
    stop.wait(0.1)
    stop.set()
    worker.join(5)
    assert nest.claims == 1  # jeder weitere Claim würde den Token entwerten


def test_401_reclaims_once_and_retries_the_call(nest):
    client = NestClient(nest.url)
    client.claim()
    nest.valid = "nestp_in_nest_rotiert"  # der Betreiber hat rotiert
    assert client.notify("nach Rotation").ok
    assert nest.claims == 2 and [n["title"] for n in nest.notified] == ["nach Rotation"]
    assert nest.calls[-3:] == ["POST /notify", "POST /claim", "POST /notify"]


def test_401_with_refused_reclaim_gives_up_without_looping(nest):
    client = NestClient(nest.url)
    client.claim()
    nest.valid = ""  # Rechte entzogen: Token tot, Claim abgelehnt
    nest.refuse = [(403, "pending")]
    res = client.notify("x")
    assert res.ok is False and "401" in res.error
    assert nest.claims == 2 and not client.configured and client.claim_error == "pending"
    assert nest.calls.count("POST /notify") == 1


def test_parallel_401s_do_not_invalidate_each_others_token(nest):
    client = NestClient(nest.url)
    client.claim()
    stale = client._token
    client.claim()  # ein anderer Thread hat schon neu geholt
    assert client.claim(stale=stale) is True
    assert nest.claims == 2 and client._token == nest.valid


def test_engine_reads_settings_after_claim_and_explains_refusal(nest):
    nest.refuse = [(403, "pending")]
    client = NestClient(nest.url)
    engine = Engine(Store(":memory:"), client, source_factory=lambda st: MultiSource({}), pause=lambda s: None)
    client.claim()
    engine.refresh_from_nest()
    assert engine.nest_status == "Rechte in Nest noch nicht akzeptiert – Benachrichtigungen sind aus"
    client.claim()
    engine.refresh_from_nest()
    assert engine.nest_status is None and [s.query for s in engine.searches] == ["aus nest"]


# ------------------------------------------------------------------ Manifest
def test_manifest_has_no_iframe_and_no_frame_action():
    text = json.dumps(MANIFEST)
    assert "iframe" not in text and '"frame"' not in text
    assert MANIFEST["scopes"] == ["notify"]
    assert all("frame" not in a for a in MANIFEST["actions"])
    kinds = {w["id"]: w["kind"] for w in MANIFEST["widgets"]}
    assert kinds == {"overview": "stat", "offers": "cards", "latest": "list", "searches": "status",
                     "deals": "table", "prices": "chart", "restock": "status", "gallery": "media"}
    g = next(w for w in MANIFEST["widgets"] if w["id"] == "gallery")
    assert g["defaultSize"] in g["sizes"] and 5 <= g["refreshSeconds"] <= 3600
    assert set(g["sizes"]) <= {"1x1", "2x1", "3x1", "2x2", "4x1", "3x2", "6x1", "4x2", "6x2"}
    assert set(kinds) == set(WIDGETS)


def test_compose_requests_exactly_the_granted_scope():
    from pathlib import Path
    compose = (Path(__file__).parent.parent / "docker-compose.yml").read_text()
    assert 'nest.plugin.scopes: "notify"' in compose and 'nest.plugin.id: arthur.tracker' in compose
    assert "NEST_PLUGIN_TOKEN" not in compose and "NEST_DASHBOARD_ORIGIN" not in compose


# ------------------------------------------------------------------ media
def valid_asset_ref(ref: str) -> bool:
    """Nests Regel (payload.go, validAssetRef/ValidAssetPath)."""
    if not ref.startswith("/") or ref.startswith("//") or len(ref) > 513:
        return False
    path = ref[1:]
    if not re.fullmatch(r"[A-Za-z0-9/._~+=-]+", path):
        return False
    segments = path.split("/")
    return segments[0] != "nest" and all(s not in ("", ".", "..") for s in segments)


class Source:
    def __init__(self):
        self.items: list[Listing] = []

    def search(self, search):
        return list(reversed(self.items))


class Client:
    configured = True

    def notify(self, *a, **k):
        from nest_plugin.nest_client import NotifyResult
        return NotifyResult(ok=True)


def listing(n: int, **extra) -> Listing:
    return Listing(item_id=str(n), title=f"ThinkPad T14 #{n}", price=400, currency="EUR",
                   url=f"https://www.ebay.de/itm/{n}", source="ebay", **extra)


def test_gallery_is_a_media_payload():
    ebay = Source()
    engine = Engine(Store(":memory:"), Client(), source_factory=lambda st: MultiSource({"ebay": ebay}),
                    pause=lambda s: None)
    assert gallery(engine, "6x2")["state"] == "empty"  # keine Suche
    engine.update_settings({"searches": ["thinkpad t14 max:600 @ebay"]}, trigger=False)
    empty = gallery(engine, "6x2")
    assert empty["kind"] == "media" and empty["state"] == "empty" and empty["message"] and empty["items"] == []

    ebay.items = [listing(1, image_url="https://i.ebayimg.com/images/g/abc/s-l140.jpg", location="Berlin")]
    engine.run_once()
    ebay.items.append(listing(2))
    engine.run_once()
    p = gallery(engine, "6x2")
    assert p["kind"] == "media" and p["state"] == "ok"
    second, first = p["items"]  # neueste zuerst
    assert second == {"id": "ebay:2", "title": "ThinkPad T14 #2", "subtitle": "400 €", "meta": "eBay · gerade eben",
                      "url": "https://www.ebay.de/itm/2", "badge": "Neu", "badgeTone": "info"}
    assert first["image"] == "/img/ebay-1" and first["subtitle"] == "400 € · Berlin"
    for i in p["items"]:
        assert set(i) <= {"id", "title", "subtitle", "image", "badge", "badgeTone", "meta", "url"}
        assert i["id"].strip() and i["title"].strip() and i["url"].startswith("https://")
        assert i.get("badgeTone", "ok") in {"ok", "warn", "error", "info"}
        assert "image" not in i or valid_asset_ref(i["image"])
    assert len(gallery(engine, "2x2")["items"]) <= 4
    json.dumps(p)


def test_gallery_image_path_is_served_by_the_image_route(tmp_path, monkeypatch):
    """Der Pfad aus dem Payload muss genau das sein, was Nests Asset-Proxy beim Plugin abholt."""
    import urllib.request
    from nest_plugin import server

    ebay = Source()
    engine = Engine(Store(":memory:"), Client(), source_factory=lambda st: MultiSource({"ebay": ebay}),
                    pause=lambda s: None)
    engine.update_settings({"searches": ["thinkpad t14 max:600 @ebay"]}, trigger=False)
    ebay.items = [listing(1, image_url="https://i.ebayimg.com/images/g/abc/s-l140.jpg")]
    engine.run_once()
    fetched: list[str] = []
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    monkeypatch.setattr(server.ImageCache, "get", lambda self, url: (fetched.append(url), (png, "image/png"))[1])
    httpd = server.serve(engine, 0, tmp_path)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        path = gallery(engine, "6x2")["items"][0]["image"]
        with urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_address[1]}{path}") as r:
            assert r.headers["Content-Type"] == "image/png" and r.read() == png
        assert fetched == ["https://i.ebayimg.com/images/g/abc/s-l400.jpg"]
    finally:
        httpd.shutdown()
