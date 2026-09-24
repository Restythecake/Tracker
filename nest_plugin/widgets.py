"""Widget-Payloads (PLUGIN-API §6): deklarativ, keine Nullen, die wie Messungen aussehen.

Regel aus §6.2, die hier überall gilt: hat der Tracker nichts zu sagen, ist das
``state: "empty"`` mit einem Satz -- nie eine 0, die aussieht, als hätte er
gemessen. "0 neue Treffer" ist nur dann eine Zahl, wenn wirklich gesucht wurde.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ebay_tracker.models import format_money

from .engine import Engine
from .store import iso, now, parse_iso

# Wie viele Zeilen in welche Größe passen, ohne dass Nest abschneiden muss.
LIST_ROWS = {"2x2": 3, "3x2": 4, "4x2": 4, "6x2": 4}
TABLE_ROWS = {"4x2": 4, "6x2": 4}


def ago(dt: datetime | None) -> str:
    if dt is None:
        return "nie"
    secs = (now() - dt).total_seconds()
    if secs < 60:
        return "gerade eben"
    if secs < 3600:
        return f"vor {int(secs // 60)} min"
    if secs < 86400:
        return f"vor {int(secs // 3600)} h"
    days = int(secs // 86400)
    return "gestern" if days == 1 else f"vor {days} Tagen"


def until(dt: datetime | None) -> str:
    if dt is None:
        return ""
    secs = (dt - now()).total_seconds()
    if secs <= 60:
        return "gleich"
    return f"in {int(secs // 60)} min"


def _no_searches() -> dict[str, Any]:
    return {"kind": "status", "state": "empty", "label": "Tracker",
            "message": "Noch keine Suche angelegt – in den Plugin-Einstellungen eine Zeile hinzufügen."}


def _warning(engine: Engine) -> str | None:
    """Der eine Satz, der erklärt, warum der Tracker gerade nicht voll funktioniert."""
    states = engine.store.all_search_states()
    failing = [name for name, row in states.items() if row["last_error"]]
    if failing:
        return f"{len(failing)} Suche(n) scheitern gerade: {', '.join(failing[:2])}"
    if engine.nest_status:
        return engine.nest_status
    bad = [p for p in engine.parsed if p.error]
    if bad:
        return f"{len(bad)} Zeile(n) nicht verstanden: {bad[0].line[:40]}"
    return None


def overview(engine: Engine, size: str | None) -> dict[str, Any]:
    if not engine.searches:
        return _no_searches()
    last = engine.store.get_meta("last_run")
    if not last:
        return {"kind": "stat", "state": "loading", "label": "Neue Treffer",
                "message": "Erste Suche läuft …"}
    count = engine.store.count_new_since(now() - timedelta(hours=24))
    active = len(engine.active_searches)
    sub = f"{active} Suche{'n' if active != 1 else ''} · geprüft {ago(parse_iso(last.get('finished')))}"
    payload: dict[str, Any] = {
        "kind": "stat", "label": "Neue Treffer · 24 h", "value": count, "sub": sub,
        "updatedAt": last.get("finished"),
    }
    warn = _warning(engine)
    if warn:
        payload["state"] = "warn"
        payload["message"] = warn
    return payload


def _subtitle(listing) -> str:
    parts = [listing.price_text, listing.source_label]
    if listing.location:
        parts.append(listing.location)
    return " · ".join(parts)


def latest(engine: Engine, size: str | None) -> dict[str, Any]:
    if not engine.searches:
        return _no_searches()
    rows = LIST_ROWS.get(size or "", 4)
    matches = engine.store.recent_matches(limit=rows)
    if not matches:
        tracked = engine.store.count_tracked()
        msg = (f"Noch nichts Neues. {tracked} passende Angebote sind schon bekannt und werden nicht erneut gemeldet."
               if tracked else "Noch nichts gefunden – die erste Suche läuft gleich.")
        return {"kind": "list", "state": "empty", "title": "Neueste Treffer", "message": msg, "items": []}
    return {
        "kind": "list", "state": "ok", "title": "Neueste Treffer",
        "items": [
            {"id": m.key + "@" + m.search, "title": m.listing.title, "subtitle": _subtitle(m.listing),
             "meta": ago(m.first_seen), "state": "ok"}
            for m in matches
        ],
    }


def searches(engine: Engine, size: str | None) -> dict[str, Any]:
    if not engine.parsed:
        return _no_searches()
    states = engine.store.all_search_states()
    day = now() - timedelta(hours=24)
    checks = []
    worst = "ok"
    for p in engine.parsed:
        if p.search is None:
            checks.append({"label": p.line[:48] or "?", "state": "error", "detail": f"Nicht verstanden: {p.error}"})
            worst = "warn"
            continue
        s = p.search
        row = states.get(s.name)
        if not s.enabled:
            checks.append({"label": s.name, "state": "empty", "detail": "pausiert"})
            continue
        if row is None or row["last_run"] is None:
            checks.append({"label": s.name, "state": "loading", "detail": "wartet auf ersten Lauf"})
            continue
        new_today = len(engine.store.recent_matches(limit=100, search=s.name, since=day))
        detail = f"{row['last_matched']} passend · {new_today} neu heute · {ago(parse_iso(row['last_run']))}"
        if row["last_error"]:
            checks.append({"label": s.name, "state": "error", "detail": row["last_error"][:120]})
            worst = "warn"
        elif row["source_errors"]:
            checks.append({"label": s.name, "state": "warn",
                           "detail": detail + " · eine Quelle hakt"})
            worst = "warn"
        else:
            checks.append({"label": s.name, "state": "ok", "detail": detail})
    nxt = until(engine.next_run_at)
    label = "Läuft gerade …" if engine.running else (f"Nächste Prüfung {nxt}" if nxt else "Suchen")
    return {"kind": "status", "state": worst, "label": label, "checks": checks,
            **({"message": _warning(engine)} if worst == "warn" and _warning(engine) else {})}


def deals(engine: Engine, size: str | None) -> dict[str, Any]:
    if not engine.searches:
        return _no_searches()
    matches = engine.store.recent_matches(limit=TABLE_ROWS.get(size or "", 6), include_silent=True)
    columns = [
        {"key": "title", "label": "Angebot"},
        {"key": "price", "label": "Preis", "align": "right"},
        {"key": "source", "label": "Quelle"},
        {"key": "age", "label": "Gefunden", "align": "right"},
    ]
    if size == "6x2":
        columns.insert(3, {"key": "search", "label": "Suche"})
    if not matches:
        return {"kind": "table", "state": "empty", "title": "Passende Angebote",
                "message": "Noch keine passenden Angebote.", "columns": columns, "rows": []}
    return {
        "kind": "table", "state": "ok", "title": "Passende Angebote", "columns": columns,
        "rows": [
            {"title": m.listing.title[:70], "price": m.listing.price_text, "source": m.listing.source_label,
             "search": m.search, "age": ago(m.first_seen)}
            for m in matches
        ],
    }


def prices(engine: Engine, size: str | None) -> dict[str, Any]:
    if not engine.searches:
        return _no_searches()
    since = now() - timedelta(days=14)
    series = []
    for s in engine.active_searches:
        points = engine.store.price_series(s.name, since)
        if points:
            series.append({"name": s.name, "points": [{"t": iso(t), "v": round(v, 2)} for t, v in points]})
    if not series:
        return {"kind": "chart", "state": "empty", "label": "Preisverlauf",
                "message": "Noch keine Preise – nach ein paar Läufen steht hier der Median je Suche.",
                "series": []}
    latest_values = [f"{x['name']}: {format_money(x['points'][-1]['v'])}" for x in series[:2]]
    return {"kind": "chart", "state": "ok", "label": "Median-Preis passender Angebote", "unit": "€",
            "title": "Preisverlauf · " + " · ".join(latest_values) if size == "6x2" else "Preisverlauf",
            "series": series}


WIDGETS = {
    "overview": overview,
    "latest": latest,
    "searches": searches,
    "deals": deals,
    "prices": prices,
}
