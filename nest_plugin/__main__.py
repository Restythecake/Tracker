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
from .server import serve
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
    # Bis 0.9 lag der Token auf dem Volume. Er kommt jetzt per Claim und bleibt im Speicher.
    (data_dir / ".nest-token").unlink(missing_ok=True)
    client = NestClient()
    engine = Engine(store, client)
    httpd = serve(engine, port, data_dir)

    stop = threading.Event()

    def shutdown(*_):
        log.info("Beende …")
        stop.set()
        engine.stop()
        BROWSER.close()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # Token holen und danach die Einstellungen -- im Hintergrund: lehnt Nest ab
    # (Rechte noch nicht akzeptiert, Nest nicht erreichbar), läuft der Tracker
    # trotzdem und versucht es jede Minute wieder.
    threading.Thread(target=client.keep_claimed, args=(stop, engine.refresh_from_nest), daemon=True).start()
    engine.start()
    log.info("Tracker-Plugin %s lauscht auf :%d", engine_version(), port)
    httpd.serve_forever()
    return 0


def engine_version() -> str:
    from . import __version__
    return __version__


if __name__ == "__main__":
    raise SystemExit(main())
