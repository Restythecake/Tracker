"""Zustand des Plugins: gesehene Treffer, Läufe je Suche, Preisverlauf, Einstellungs-Cache.

Absichtlich eine lokale SQLite-Datei auf einem Volume und nicht Nests KV-Speicher
(``storage:kv``): der hat 256 Schlüssel und 64 KiB je Wert, und ein Tracker, der
seit Wochen Anzeigen sammelt, sprengt das. Ein Scope weniger ist außerdem eine
Freigabe weniger, die der Betreiber verstehen muss.
"""
from __future__ import annotations

import json
import sqlite3
import statistics
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ebay_tracker.models import Listing

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    key         TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    item_id     TEXT NOT NULL,
    title       TEXT NOT NULL,
    price       REAL,
    price_note  TEXT,
    currency    TEXT,
    url         TEXT NOT NULL,
    image_url   TEXT,
    location    TEXT,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS matches (
    search      TEXT NOT NULL,
    key         TEXT NOT NULL,
    first_seen  TEXT NOT NULL,
    notified    INTEGER NOT NULL DEFAULT 0,
    silent      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (search, key)
);
CREATE INDEX IF NOT EXISTS matches_first_seen ON matches(first_seen);
CREATE TABLE IF NOT EXISTS searches (
    name            TEXT PRIMARY KEY,
    signature       TEXT NOT NULL,
    initialized_at  TEXT,
    init_sources    TEXT,
    last_run        TEXT,
    last_ok         TEXT,
    last_error      TEXT,
    source_errors   TEXT,
    last_fetched    INTEGER DEFAULT 0,
    last_matched    INTEGER DEFAULT 0,
    last_new        INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    search        TEXT NOT NULL,
    at            TEXT NOT NULL,
    fetched       INTEGER NOT NULL,
    matched       INTEGER NOT NULL,
    new           INTEGER NOT NULL,
    median_price  REAL,
    min_price     REAL,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS runs_search_at ON runs(search, at);
CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


@dataclass
class Match:
    search: str
    key: str
    first_seen: datetime
    notified: bool
    silent: bool
    listing: Listing


class Store:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # Ein Server-Thread je Anfrage plus der Scheduler: eine Verbindung, ein Lock.
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ meta
    def get_meta(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_meta(self, key: str, value: Any) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )
            self.conn.commit()

    # -------------------------------------------------------------- searches
    def search_state(self, name: str) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute("SELECT * FROM searches WHERE name = ?", (name,)).fetchone()

    def initialized_sources(self, name: str, signature: str) -> set[str]:
        """Quellen, die für diese Suche schon einmal still eingelesen wurden.

        Neu oder geändert -> leer, der nächste Lauf merkt sich nur, statt zu
        melden: Wer den Höchstpreis anhebt, will keine 40 Meldungen über
        Anzeigen, die seit Tagen online sind. Und zwar *je Quelle*: war eBay
        beim ersten Lauf gesperrt, darf der erste erfolgreiche eBay-Lauf später
        nicht alles, was dort schon steht, als neu melden.
        """
        row = self.search_state(name)
        if row is None or row["signature"] != signature or not row["init_sources"]:
            return set()
        return set(json.loads(row["init_sources"]))

    def record_run(self, name: str, signature: str, *, fetched: int, matched: int, new: int,
                   prices: list[float], error: str | None, source_errors: dict[str, str],
                   answered: set[str]) -> None:
        at = iso(now())
        with self._lock:
            done = self.initialized_sources(name, signature) | answered
            row = self.search_state(name)
            init_at = row["initialized_at"] if row and row["signature"] == signature else None
            if done and init_at is None:
                init_at = at
            self.conn.execute(
                """INSERT INTO searches (name, signature, initialized_at, init_sources, last_run, last_ok,
                       last_error, source_errors, last_fetched, last_matched, last_new)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                       signature = excluded.signature, initialized_at = excluded.initialized_at,
                       init_sources = excluded.init_sources, last_run = excluded.last_run,
                       last_ok = COALESCE(excluded.last_ok, searches.last_ok),
                       last_error = excluded.last_error, source_errors = excluded.source_errors,
                       last_fetched = excluded.last_fetched, last_matched = excluded.last_matched,
                       last_new = excluded.last_new""",
                (name, signature, init_at, json.dumps(sorted(done)), at, None if error else at, error,
                 json.dumps(source_errors) if source_errors else None, fetched, matched, new),
            )
            self.conn.execute(
                "INSERT INTO runs (search, at, fetched, matched, new, median_price, min_price, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (name, at, fetched, matched, new,
                 statistics.median(prices) if prices else None, min(prices) if prices else None, error),
            )
            self.conn.commit()

    def forget_searches_except(self, names: set[str]) -> None:
        """Gelöschte Suchen verschwinden aus den Widgets; ihre Treffer bleiben in der Historie."""
        with self._lock:
            rows = self.conn.execute("SELECT name FROM searches").fetchall()
            for row in rows:
                if row["name"] not in names:
                    self.conn.execute("DELETE FROM searches WHERE name = ?", (row["name"],))
            self.conn.commit()

    def all_search_states(self) -> dict[str, sqlite3.Row]:
        with self._lock:
            return {r["name"]: r for r in self.conn.execute("SELECT * FROM searches").fetchall()}

    # -------------------------------------------------------------- listings
    def upsert_listing(self, listing: Listing) -> None:
        at = iso(now())
        with self._lock:
            self.conn.execute(
                """INSERT INTO listings (key, source, item_id, title, price, price_note, currency, url,
                       image_url, location, first_seen, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                       title = excluded.title, price = excluded.price, price_note = excluded.price_note,
                       image_url = COALESCE(excluded.image_url, listings.image_url),
                       location = COALESCE(excluded.location, listings.location),
                       last_seen = excluded.last_seen""",
                (listing.key, listing.source, listing.item_id, listing.title, listing.price,
                 listing.price_note, listing.currency, listing.url, listing.image_url,
                 listing.location, at, at),
            )

    def is_matched(self, search: str, key: str) -> bool:
        with self._lock:
            return self.conn.execute(
                "SELECT 1 FROM matches WHERE search = ? AND key = ?", (search, key)
            ).fetchone() is not None

    def add_match(self, search: str, listing: Listing, *, silent: bool) -> None:
        with self._lock:
            self.upsert_listing(listing)
            self.conn.execute(
                "INSERT OR IGNORE INTO matches (search, key, first_seen, notified, silent) VALUES (?, ?, ?, 0, ?)",
                (search, listing.key, iso(now()), int(silent)),
            )

    def mark_notified(self, search: str, key: str) -> None:
        with self._lock:
            self.conn.execute("UPDATE matches SET notified = 1 WHERE search = ? AND key = ?", (search, key))

    def commit(self) -> None:
        with self._lock:
            self.conn.commit()

    def listing(self, key: str) -> Listing | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM listings WHERE key = ?", (key,)).fetchone()
        return _listing(row) if row else None

    def recent_matches(self, *, limit: int = 50, search: str | None = None,
                       include_silent: bool = False, since: datetime | None = None) -> list[Match]:
        sql = ("SELECT m.search, m.key, m.first_seen, m.notified, m.silent, l.* FROM matches m "
               "JOIN listings l ON l.key = m.key WHERE 1=1")
        args: list[Any] = []
        if not include_silent:
            sql += " AND m.silent = 0"
        if search:
            sql += " AND m.search = ?"
            args.append(search)
        if since:
            sql += " AND m.first_seen >= ?"
            args.append(iso(since))
        sql += " ORDER BY m.first_seen DESC, l.rowid DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self.conn.execute(sql, args).fetchall()
        return [
            Match(search=r["search"], key=r["key"], first_seen=parse_iso(r["first_seen"]),
                  notified=bool(r["notified"]), silent=bool(r["silent"]), listing=_listing(r))
            for r in rows
        ]

    def count_new_since(self, since: datetime) -> int:
        with self._lock:
            return self.conn.execute(
                "SELECT COUNT(DISTINCT key) FROM matches WHERE silent = 0 AND first_seen >= ?", (iso(since),)
            ).fetchone()[0]

    def count_tracked(self, search: str | None = None) -> int:
        with self._lock:
            if search:
                return self.conn.execute("SELECT COUNT(*) FROM matches WHERE search = ?", (search,)).fetchone()[0]
            return self.conn.execute("SELECT COUNT(DISTINCT key) FROM matches").fetchone()[0]

    def price_series(self, search: str, since: datetime) -> list[tuple[datetime, float]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT at, median_price FROM runs WHERE search = ? AND at >= ? AND median_price IS NOT NULL "
                "ORDER BY at", (search, iso(since))
            ).fetchall()
        return [(parse_iso(r["at"]), r["median_price"]) for r in rows]

    def reset(self) -> None:
        """Vergisst Treffer und Läufe; der nächste Lauf ist wieder still."""
        with self._lock:
            self.conn.executescript("DELETE FROM matches; DELETE FROM runs; DELETE FROM searches; DELETE FROM listings;")
            self.conn.commit()

    def prune(self, keep_days: int = 60) -> None:
        cutoff = iso(now() - timedelta(days=keep_days))
        with self._lock:
            self.conn.execute("DELETE FROM runs WHERE at < ?", (cutoff,))
            self.conn.execute("DELETE FROM matches WHERE first_seen < ?", (cutoff,))
            self.conn.execute("DELETE FROM listings WHERE key NOT IN (SELECT key FROM matches)")
            self.conn.commit()


def _listing(row: sqlite3.Row) -> Listing:
    return Listing(
        item_id=row["item_id"], title=row["title"], price=row["price"], price_note=row["price_note"],
        currency=row["currency"] or "EUR", url=row["url"], image_url=row["image_url"],
        location=row["location"], source=row["source"],
    )
