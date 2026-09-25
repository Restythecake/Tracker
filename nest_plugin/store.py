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
CREATE TABLE IF NOT EXISTS observed (
    key    TEXT PRIMARY KEY,
    gpu    TEXT NOT NULL,
    price  REAL NOT NULL,
    at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS observed_gpu ON observed(gpu, at);
CREATE TABLE IF NOT EXISTS restock (
    shop        TEXT PRIMARY KEY,
    url         TEXT,
    name        TEXT,
    price       REAL,
    available   INTEGER,          -- 0/1; NULL = unklar
    changed_at  TEXT,
    note        TEXT
);
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
    notified: int  # 0 nein, 1 per Push, 2 beim Einlesen bekannt
    silent: int
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
            self._migrate()

    # Spalten, die nach 0.1.0 dazukamen. CREATE TABLE IF NOT EXISTS legt sie
    # auf einem bestehenden Volume nicht an.
    _LISTING_COLUMNS = {"description": "TEXT", "dealer": "INTEGER DEFAULT 0", "shipping_note": "TEXT",
                        "condition": "TEXT", "assessment": "TEXT", "seller_since": "TEXT",
                        "details_loaded": "INTEGER DEFAULT 0", "is_new": "INTEGER"}

    def _migrate(self) -> None:
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(listings)")}
        for col, kind in self._LISTING_COLUMNS.items():
            if col not in have:
                self.conn.execute(f"ALTER TABLE listings ADD COLUMN {col} {kind}")
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(searches)")}
        if "init_sources" not in have:
            self.conn.execute("ALTER TABLE searches ADD COLUMN init_sources TEXT")
        self.conn.commit()

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

    def drop_matches(self, search: str) -> None:
        """Löscht die Treffer einer Suche (ohne den Beobachtungs-Preise).

        Eine geänderte Suche beginnt ein neues Leben: die alte Trefferliste
        (z. B. aus einem anderen Gebiet) gehört nicht mehr dazu, und in der
        Galerie sollen keine Anzeigen eines Umkreises hängen, der längst
        anders eingestellt ist. Der nächste Lauf liest still neu ein.
        """
        with self._lock:
            self.conn.execute("DELETE FROM matches WHERE search = ?", (search,))
            self.conn.execute("DELETE FROM listings WHERE key NOT IN (SELECT key FROM matches)")
            self.conn.execute("DELETE FROM runs WHERE search = ?", (search,))
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
        assessment = json.dumps(listing.assessment, ensure_ascii=False) if listing.assessment else None
        is_new = None if listing.is_new is None else int(listing.is_new)
        with self._lock:
            self.conn.execute(
                """INSERT INTO listings (key, source, item_id, title, price, price_note, currency, url,
                       image_url, location, first_seen, last_seen, description, dealer, shipping_note,
                       condition, assessment, seller_since, details_loaded, is_new)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                       title = excluded.title, price = excluded.price, price_note = excluded.price_note,
                       image_url = COALESCE(excluded.image_url, listings.image_url),
                       location = COALESCE(excluded.location, listings.location),
                       description = COALESCE(excluded.description, listings.description),
                       dealer = MAX(excluded.dealer, listings.dealer),
                       shipping_note = COALESCE(excluded.shipping_note, listings.shipping_note),
                       condition = COALESCE(excluded.condition, listings.condition),
                       assessment = COALESCE(excluded.assessment, listings.assessment),
                       seller_since = COALESCE(excluded.seller_since, listings.seller_since),
                       details_loaded = MAX(excluded.details_loaded, listings.details_loaded),
                       is_new = COALESCE(excluded.is_new, listings.is_new),
                       last_seen = excluded.last_seen""",
                (listing.key, listing.source, listing.item_id, listing.title, listing.price,
                 listing.price_note, listing.currency, listing.url, listing.image_url,
                 listing.location, at, at, listing.description, int(listing.dealer), listing.shipping_note,
                 listing.condition, assessment, listing.seller_since, int(listing.details_loaded), is_new),
            )

    def known_listing(self, key: str) -> bool:
        with self._lock:
            return self.conn.execute("SELECT 1 FROM listings WHERE key = ?", (key,)).fetchone() is not None

    def observe(self, key: str, gpu: str, price: float) -> None:
        """Merkt sich einen Angebotspreis je GPU für den Marktvergleich (auch für nicht passende)."""
        with self._lock:
            self.conn.execute(
                "INSERT INTO observed (key, gpu, price, at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET price = excluded.price, gpu = excluded.gpu",
                (key, gpu, price, iso(now())),
            )

    def market_prices(self, gpu: str, days: int = 21) -> list[float]:
        with self._lock:
            rows = self.conn.execute("SELECT price FROM observed WHERE gpu = ? AND at >= ?",
                                     (gpu, iso(now() - timedelta(days=days)))).fetchall()
        return [r["price"] for r in rows]

    def is_matched(self, search: str, key: str) -> bool:
        with self._lock:
            return self.conn.execute(
                "SELECT 1 FROM matches WHERE search = ? AND key = ?", (search, key)
            ).fetchone() is not None

    def restock_rows(self) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute("SELECT * FROM restock ORDER BY shop").fetchall()

    def update_restock(self, shop: str, *, url: str, name: str, price: float | None,
                       available: bool | None, note: str | None) -> bool:
        """Speichert den Zustand; True, wenn er sich geändert hat."""
        with self._lock:
            row = self.conn.execute("SELECT available, price, note FROM restock WHERE shop = ?", (shop,)).fetchone()
            changed = row is None or row["available"] != (None if available is None else int(available))
            self.conn.execute(
                """INSERT INTO restock (shop, url, name, price, available, changed_at, note)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(shop) DO UPDATE SET url = excluded.url, name = excluded.name,
                       price = excluded.price, available = excluded.available,
                       changed_at = excluded.changed_at, note = excluded.note""",
                (shop, url, name, price, None if available is None else int(available),
                 iso(now()) if changed else None, note),
            )
            self.conn.commit()
            return changed

    # matches.silent:   0 = Deal/meldbar, 1 = passt, aber kein Deal (Galerie),
    #                   2 = versteckt (nach Detailprüfung ungeeignet)
    # matches.notified: 0 = noch nicht gemeldet, 1 = per Push gemeldet,
    #                   2 = beim stillen Einlesen schon da gewesen ("bekannt")
    def add_match(self, search: str, listing: Listing, *, silent: bool | int, known: bool = False) -> None:
        with self._lock:
            self.upsert_listing(listing)
            self.conn.execute(
                """INSERT INTO matches (search, key, first_seen, notified, silent) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(search, key) DO UPDATE SET silent = excluded.silent""",
                (search, listing.key, iso(now()), 2 if known else 0, int(silent)),
            )

    def hide_matches(self, search: str, keys: list[str]) -> int:
        """Treffer, die bei der aktuellen Bewertung durchfallen, aus der Anzeige nehmen."""
        if not keys:
            return 0
        with self._lock:
            cur = self.conn.execute(
                f"UPDATE matches SET silent = 2 WHERE search = ? AND silent != 2 AND key IN ({','.join('?' * len(keys))})",
                (search, *keys))
            return cur.rowcount

    def match_row(self, search: str, key: str) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute("SELECT silent, notified, first_seen FROM matches WHERE search = ? AND key = ?",
                                     (search, key)).fetchone()

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
                       include_silent: bool = False, since: datetime | None = None,
                       searches: list[str] | None = None) -> list[Match]:
        sql = ("SELECT m.search, m.key, m.first_seen, m.notified, m.silent, l.* FROM matches m "
               "JOIN listings l ON l.key = m.key WHERE 1=1")
        args: list[Any] = []
        sql += " AND m.silent = 0" if not include_silent else " AND m.silent < 2"
        if search:
            sql += " AND m.search = ?"
            args.append(search)
        elif searches is not None:
            sql += f" AND m.search IN ({','.join('?' * len(searches)) or 'NULL'})"
            args.extend(searches)
        if since:
            sql += " AND m.first_seen >= ?"
            args.append(iso(since))
        sql += " ORDER BY m.first_seen DESC, l.rowid DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self.conn.execute(sql, args).fetchall()
        return [
            Match(search=r["search"], key=r["key"], first_seen=parse_iso(r["first_seen"]),
                  notified=int(r["notified"]), silent=int(r["silent"]), listing=_listing(r))
            for r in rows
        ]

    def count_new_since(self, since: datetime, searches: list[str] | None = None) -> int:
        sql = "SELECT COUNT(DISTINCT key) FROM matches WHERE silent = 0 AND notified != 2 AND first_seen >= ?"
        args: list[Any] = [iso(since)]
        if searches is not None:
            sql += f" AND search IN ({','.join('?' * len(searches)) or 'NULL'})"
            args.extend(searches)
        with self._lock:
            return self.conn.execute(sql, args).fetchone()[0]

    def count_tracked(self, search: str | None = None) -> int:
        with self._lock:
            if search:
                return self.conn.execute("SELECT COUNT(*) FROM matches WHERE search = ? AND silent < 2", (search,)).fetchone()[0]
            return self.conn.execute("SELECT COUNT(DISTINCT key) FROM matches WHERE silent < 2").fetchone()[0]

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
            self.conn.executescript("DELETE FROM matches; DELETE FROM runs; DELETE FROM searches; DELETE FROM listings; DELETE FROM observed;")
            self.conn.commit()

    def prune(self, keep_days: int = 60) -> None:
        cutoff = iso(now() - timedelta(days=keep_days))
        with self._lock:
            self.conn.execute("DELETE FROM runs WHERE at < ?", (cutoff,))
            self.conn.execute("DELETE FROM matches WHERE first_seen < ?", (cutoff,))
            self.conn.execute("DELETE FROM listings WHERE key NOT IN (SELECT key FROM matches)")
            self.conn.execute("DELETE FROM observed WHERE at < ?", (cutoff,))
            self.conn.commit()


def _listing(row: sqlite3.Row) -> Listing:
    keys = row.keys()

    def col(name: str) -> Any:
        return row[name] if name in keys else None

    raw = col("assessment")
    is_new = col("is_new")
    return Listing(
        item_id=row["item_id"], title=row["title"], price=row["price"], price_note=row["price_note"],
        currency=row["currency"] or "EUR", url=row["url"], image_url=row["image_url"],
        location=row["location"], source=row["source"], description=col("description"),
        dealer=bool(col("dealer")), shipping_note=col("shipping_note"), condition=col("condition"),
        assessment=json.loads(raw) if raw else None, seller_since=col("seller_since"),
        details_loaded=bool(col("details_loaded")), is_new=None if is_new is None else bool(is_new),
    )
