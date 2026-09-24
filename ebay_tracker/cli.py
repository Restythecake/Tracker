from __future__ import annotations

import argparse
import logging
import sys

from .config import ConfigError, load_config
from .models import Listing
from .notifiers import build_notifiers, notify_all
from .sources import SourceError, create_source
from .storage import Storage
from .tracker import Tracker, dry_run


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ebay_tracker", description="Überwacht eBay nach gewünschten Configs.")
    parser.add_argument("-c", "--config", default="config.yaml", help="Pfad zur Konfiguration (Standard: config.yaml)")
    parser.add_argument("-v", "--verbose", action="store_true", help="ausführliche Ausgabe (zeigt auch verworfene Angebote)")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="Dauerhaft überwachen (Standard)")
    run.add_argument("--once", action="store_true", help="nur einen Durchlauf (z. B. für cron / GitHub Actions)")
    sub.add_parser("check", help="Probelauf: zeigt aktuelle Treffer, ohne zu speichern oder zu melden")
    sub.add_parser("test-notify", help="Schickt eine Testnachricht an alle Kanäle")

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    log = logging.getLogger("ebay_tracker")

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    command = args.command or "run"
    notifiers = build_notifiers(config.notifiers)

    if command == "test-notify":
        listing = Listing(
            item_id="0", title="Testnachricht vom eBay-Tracker – wenn du das siehst, klappt's!",
            price=999.0, currency=config.settings.currency, url=f"https://{config.settings.domain}",
        )
        log.info("Aktive Kanäle: %s", ", ".join(n.name for n in notifiers) or "keine")
        return 0 if notify_all(notifiers, "Test", listing) else 1

    try:
        source = create_source(config.settings)
    except SourceError as exc:
        log.error("%s", exc)
        return 2

    if command == "check":
        for search, listing, reason in dry_run(config, source):
            mark = "✅" if reason is None else "  "
            suffix = "" if reason is None else f"   ({reason})"
            print(f"{mark} [{search.name}] {listing.price_text:>18}  {listing.title[:80]}{suffix}")
            if reason is None:
                print(f"      {listing.url}")
        return 0

    log.info("Benachrichtigungen über: %s", ", ".join(n.name for n in notifiers) or "keine!")
    storage = Storage(config.settings.database)
    tracker = Tracker(config, source, storage, notifiers)
    try:
        if getattr(args, "once", False):
            results = tracker.check_all()
            return 1 if results and all(r.error for r in results) else 0
        tracker.run_forever()
    except KeyboardInterrupt:
        log.info("Beendet.")
    finally:
        storage.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
