"""Merkt sich gesehene Angebote, damit jede Meldung nur einmal kommt."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import Listing

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    search_name TEXT NOT NULL,
    item_id     TEXT NOT NULL,
    title       TEXT,
    price       REAL,
    url         TEXT,
    first_seen  TEXT NOT NULL,
    notified    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (search_name, item_id)
);
CREATE TABLE IF NOT EXISTS searches (
    name           TEXT PRIMARY KEY,
    initialized_at TEXT NOT NULL
);
"""


class Storage:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.executescript(SCHEMA)

    def is_initialized(self, search_name: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM searches WHERE name = ?", (search_name,)).fetchone()
        return row is not None

    def mark_initialized(self, search_name: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO searches (name, initialized_at) VALUES (?, ?)",
            (search_name, _now()),
        )
        self.conn.commit()

    def is_seen(self, search_name: str, item_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM seen WHERE search_name = ? AND item_id = ?", (search_name, item_id)
        ).fetchone()
        return row is not None

    def mark_seen(self, search_name: str, listing: Listing, notified: bool) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO seen (search_name, item_id, title, price, url, first_seen, notified) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                search_name,
                listing.item_id,
                listing.title,
                listing.price,
                listing.url,
                _now(),
                int(notified),
            ),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
