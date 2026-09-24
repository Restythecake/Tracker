"""Startet das Plugin: HTTP-Server auf $PORT plus Scheduler-Thread."""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from pathlib import Path

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
    log = logging.getLogger("nest_plugin")
    port = int(os.environ.get("PORT", "8080"))
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    data_dir.mkdir(parents=True, exist_ok=True)

    store = Store(data_dir / "tracker.db")
    client = NestClient()
    engine = Engine(store, client)
    httpd = serve(engine, port, data_dir)

    def shutdown(*_):
        log.info("Beende …")
        engine.stop()
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


def engine_version() -> str:
    from . import __version__
    return __version__


if __name__ == "__main__":
    raise SystemExit(main())
