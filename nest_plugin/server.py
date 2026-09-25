"""HTTP-Seite des Plugins: Nests Vertrag (§4.1) plus die Frame-Oberfläche.

Zwei Zuhörer auf einem Port:

* ``/nest/v1/*`` und ``/icons/*`` -- ruft Nest über das Docker-Netz auf.
* ``/``, ``/img/*``, ``/go/*`` -- der Browser, durchgereicht von Nest über den
  Plugin-Host (``plugin-arthur-tracker.mini.shibusa.moe``), nur GET/HEAD.

Die Widget-Anfragen tragen die Einstellungen im Body mit (§4.2). Die Engine
übernimmt sie von dort, damit das Plugin auch ohne Token (also ohne ``/self``)
immer mit den aktuellen Suchen arbeitet.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from . import frame
from .engine import Engine
from .manifest import MANIFEST
from .widgets import WIDGETS

log = logging.getLogger(__name__)

ICON_DIR = Path(__file__).parent / "icons"
IMAGE_HOSTS = ("i.ebayimg.com", "img.kleinanzeigen.de", ".vinted.net")
MAX_IMAGE = 2 * 1024 * 1024
MAX_BODY = 1024 * 1024


class ImageCache:
    """Vorschaubilder der Angebote, auf Platte gecacht.

    Der Frame darf nur Bilder der eigenen Origin laden (CSP ``img-src 'self'``),
    also holt das Plugin sie und liefert sie selbst aus. Nur von den zwei
    bekannten Bild-CDNs -- sonst wäre ``/img`` ein offener Proxy.
    """

    def __init__(self, directory: Path, max_files: int = 800):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self.max_files = max_files
        self._lock = threading.Lock()

    def get(self, url: str) -> tuple[bytes, str] | None:
        host = urlsplit(url).hostname or ""
        allowed = any(host == h or (h.startswith(".") and host.endswith(h)) for h in IMAGE_HOSTS)
        if urlsplit(url).scheme != "https" or not allowed:
            return None
        name = hashlib.sha256(url.encode()).hexdigest()[:32]
        path = self.dir / name
        if path.exists():
            os.utime(path)
            return path.read_bytes(), _sniff(path.read_bytes()[:16])
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh) Safari/605.1.15",
                                                   "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read(MAX_IMAGE + 1)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            log.debug("Bild %s nicht ladbar: %s", url, exc)
            return None
        if len(data) > MAX_IMAGE or not data:
            return None
        mime = _sniff(data[:16])
        if not mime.startswith("image/"):
            return None
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        self._evict()
        return data, mime

    def _evict(self) -> None:
        with self._lock:
            files = sorted(self.dir.glob("*"), key=lambda p: p.stat().st_mtime)
            for old in files[: max(0, len(files) - self.max_files)]:
                old.unlink(missing_ok=True)


def card_image_url(url: str) -> str:
    """Suchseiten liefern Briefmarken (eBay 140 px, Kleinanzeigen $_2); die
    Karten sind 200-280 px breit. Beide CDNs haben feste Größenstufen in der URL."""
    url = re.sub(r"/s-l\d+\.(webp|jpg|jpeg|png)$", r"/s-l400.\1", url)
    return re.sub(r"rule=\$_\d+\.AUTO", "rule=$_59.AUTO", url)


def _sniff(head: bytes) -> str:
    if head.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG"):
        return "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[4:12] in (b"ftypavif", b"ftypheic"):
        return "image/avif"
    return "application/octet-stream"


def make_handler(engine: Engine, images: ImageCache):
    class Handler(BaseHTTPRequestHandler):
        server_version = "nest-tracker"
        protocol_version = "HTTP/1.1"

        # ------------------------------------------------------ plumbing
        def _send(self, code: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code: int, doc: Any) -> None:
            self._send(code, json.dumps(doc, ensure_ascii=False).encode(), "application/json; charset=utf-8",
                       {"Cache-Control": "no-store"})

        def _html(self, text: str, code: int = 200) -> None:
            self._send(code, text.encode(), "text/html; charset=utf-8", {"Cache-Control": "no-store"})

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            if length > MAX_BODY:
                raise ValueError("body too large")
            raw = self.rfile.read(length)
            doc = json.loads(raw or b"{}")
            return doc if isinstance(doc, dict) else {}

        def log_message(self, fmt: str, *args: Any) -> None:  # Zugriffe nur im Debug
            log.debug("%s %s", self.address_string(), fmt % args)

        # ------------------------------------------------------ GET
        def do_HEAD(self) -> None:
            self.do_GET()

        def do_GET(self) -> None:
            url = urlsplit(self.path)
            path = url.path
            params = dict(parse_qsl(url.query))
            try:
                if path == "/nest/v1/manifest":
                    return self._json(200, MANIFEST)
                if path == "/nest/v1/health":
                    return self._json(200, {"status": "ok"})
                if path.startswith("/icons/"):
                    name = Path(unquote(path)).name
                    icon = ICON_DIR / name
                    if name.endswith(".svg") and icon.is_file():
                        return self._send(200, icon.read_bytes(), "image/svg+xml")
                    return self._json(404, {"error": "no such icon"})
                if path == "/":
                    # Im Pfad-Modus (Frame unter Nests Host) setzt Nest der
                    # Antwort X-Nest-Frame-Base auf den Pfad, unter dem der
                    # Frame liegt; die Oberfläche präfixiert alle relativen
                    # URLs damit. Auf der Subdomain fehlt der Header.
                    base = frame.frame_base(self.headers)
                    if params.get("layout") == "page":
                        return self._html(frame.page(engine, params, base=base))
                    return self._html(frame.widget(engine, params, base=base))
                if path.startswith("/img/"):
                    raw = unquote(path[len("/img/"):])
                    # Neue Form "ebay-123" (Nest-Asset-Proxy), alte "ebay:123" (Galerie)
                    key = raw if ":" in raw else raw.replace("-", ":", 1)
                    listing = engine.store.listing(key)
                    got = None
                    if listing and listing.image_url:
                        got = images.get(card_image_url(listing.image_url)) or images.get(listing.image_url)
                    if not got:
                        return self._send(404, b"", "text/plain")
                    data, mime = got
                    # Bilder ändern sich nicht; Nest setzt no-store auf dem Frame-Host
                    # trotzdem -- der Plattencache hier ist der, der zählt.
                    return self._send(200, data, mime, {"Cache-Control": "private, max-age=86400"})
                if path.startswith("/go/"):
                    listing = engine.store.listing(unquote(path[len("/go/"):]))
                    return self._html(frame.go(listing.url if listing else None))
                if path == "/favicon.ico":
                    return self._send(204, b"", "text/plain")
                return self._json(404, {"error": "not found"})
            except Exception:
                log.exception("GET %s fehlgeschlagen", path)
                return self._json(500, {"error": "internal"})

        # ------------------------------------------------------ POST
        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            try:
                body = self._body()
            except (ValueError, json.JSONDecodeError):
                return self._json(400, {"error": "invalid json"})
            try:
                if m := re.fullmatch(r"/nest/v1/widgets/([A-Za-z][\w-]*)/data", path):
                    engine.update_settings(body.get("settings"))
                    build = WIDGETS.get(m.group(1))
                    if build is None:
                        return self._json(404, {"error": "unknown widget"})
                    return self._json(200, build(engine, body.get("size")))
                if m := re.fullmatch(r"/nest/v1/actions/([A-Za-z][\w-]*)", path):
                    return self._json(200, self._action(m.group(1)))
                if path == "/nest/v1/settings/changed":
                    engine.update_settings(body.get("settings"))
                    return self._json(200, {"ok": True})
                if path == "/nest/v1/events":
                    # Keine Ereignisse abonniert (kein events:read); ein Aufruf
                    # hier ist also nicht von Nest und wird nicht bestätigt.
                    return self._json(404, {"error": "no events subscribed"})
                return self._json(404, {"error": "not found"})
            except Exception:
                log.exception("POST %s fehlgeschlagen", path)
                return self._json(500, {"error": "internal"})

        def _action(self, action: str) -> dict[str, Any]:
            if action == "check-now":
                if not engine.active_searches:
                    return {"ok": False, "message": "Keine aktive Suche – erst in den Einstellungen anlegen."}
                if engine.running:
                    return {"ok": True, "message": "Läuft bereits.", "level": "info"}
                done = threading.Event()
                result: dict[str, Any] = {}

                def work() -> None:
                    try:
                        result["summary"] = engine.run_once()
                    finally:
                        done.set()

                threading.Thread(target=work, daemon=True).start()
                # Docker-Aktionen haben 10 s (§4.3). Was bis dahin fertig ist,
                # wird berichtet; der Rest läuft im Hintergrund weiter.
                if not done.wait(8):
                    return {"ok": True, "message": "Prüfung läuft im Hintergrund weiter …",
                            "refresh": list(WIDGETS), "level": "info"}
                s = result.get("summary")
                if s is None:
                    return {"ok": False, "message": "Prüfung fehlgeschlagen – siehe Plugin-Log."}
                msg = f"{s.searches} Suche(n) geprüft, {len(s.new)} neu"
                if s.errors:
                    msg += f", {len(s.errors)} mit Fehler"
                return {"ok": True, "message": msg, "refresh": list(WIDGETS),
                        "level": "warning" if s.errors else "success"}
            if action == "test-notify":
                if not engine.client.configured:
                    return {"ok": False, "message": "Kein Token im Container – in Nest „Token übernehmen“ ausführen."}
                res = engine.client.notify("Tracker ist verbunden",
                                           "So sieht eine Treffer-Meldung aus. Ein Tipp öffnet das Angebot.",
                                           level=engine.settings.notify_level,
                                           url="https://www.kleinanzeigen.de/")
                if not res.ok:
                    return {"ok": False, "message": f"Nest hat abgelehnt: {res.error}"}
                if res.suppressed:
                    return {"ok": True, "message": "Gesendet, aber von einer Nest-Regel unterdrückt.", "level": "warning"}
                return {"ok": True, "message": "Testbenachrichtigung gesendet.", "level": "success"}
            if action == "reset":
                engine.store.reset()
                engine.trigger()
                return {"ok": True, "message": "Verlauf gelöscht. Der nächste Lauf merkt sich still neu.",
                        "refresh": list(WIDGETS), "level": "success"}
            return {"ok": False, "message": f"Unbekannte Aktion {action}"}

    return Handler


def serve(engine: Engine, port: int, data_dir: Path) -> ThreadingHTTPServer:
    images = ImageCache(data_dir / "img")
    httpd = ThreadingHTTPServer(("", port), make_handler(engine, images))
    httpd.daemon_threads = True
    return httpd


def wait_for_settings(engine: Engine, attempts: int = 3) -> None:
    for i in range(attempts):
        engine.refresh_from_nest()
        if engine.nest_status is None or "Token" in (engine.nest_status or "") or "Scope" in (engine.nest_status or ""):
            return
        time.sleep(2 * (i + 1))
