"""Startet das Plugin: HTTP-Server auf $PORT plus Scheduler-Thread."""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from pathlib import Path

from ebay_tracker.sources import parsing
from ebay_tracker.sources.fetch import BROWSER

from .engine import Engine
from .nest_client import NestClient
from .server import serve, wait_for_settings
from .store import Store


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    # Scrapling meldet jeden Abruf doppelt auf INFO; unsere Zeilen ("per Browser gerettet") reichen.
    logging.getLogger("scrapling").setLevel(logging.WARNING)
    log = logging.getLogger("nest_plugin")
    port = int(os.environ.get("PORT", "8080"))
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    data_dir.mkdir(parents=True, exist_ok=True)

    # Fingerabdrücke der Trefferkarten für Scraplings Selbstheilung (sources/parsing.py)
    parsing.configure(str(data_dir / "scrapling.db") if parsing.HAS_SELECTOR else None)
    store = Store(data_dir / "tracker.db")
    client = NestClient(token=persisted_token(data_dir))
    engine = Engine(store, client)
    httpd = serve(engine, port, data_dir)

    def shutdown(*_):
        log.info("Beende …")
        engine.stop()
        BROWSER.close()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # Einstellungen holen, bevor der erste Lauf startet -- im Hintergrund,
    # damit Nests Manifest-Abfrage beim Discovery nicht auf Nest selbst wartet.
    threading.Thread(target=wait_for_settings, args=(engine,), daemon=True).start()
    engine.start()
    log.info("Tracker-Plugin %s lauscht auf :%d (Token: %s)", engine_version(), port,
             "ja" if client.configured else "nein")
    httpd.serve_forever()
    return 0


def persisted_token(data_dir: Path) -> str:
    """Token aus der Umgebung, sonst der zuletzt gesehene vom Volume.

    Nest schreibt den Token per apply-token in die Container-Umgebung. Ein
    `docker compose up --build` baut den Container aber aus der Compose-Datei
    neu -- ohne Token. Ihn auf dem eigenen Volume zu merken heißt: ein Deploy
    braucht keine Token-Rotation mehr. Datei 0600, nur für den Plugin-User."""
    path = data_dir / ".nest-token"
    token = os.environ.get("NEST_PLUGIN_TOKEN", "").strip()
    if token:
        try:
            if not path.exists() or path.read_text().strip() != token:
                path.write_text(token)
                path.chmod(0o600)
        except OSError:
            pass
        return token
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def engine_version() -> str:
    from . import __version__
    return __version__


if __name__ == "__main__":
    raise SystemExit(main())
