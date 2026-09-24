"""Widget-Payloads (PLUGIN-API §6): deklarativ, keine Nullen, die wie Messungen aussehen.

Regel aus §6.2, die hier überall gilt: hat der Tracker nichts zu sagen, ist das
``state: "empty"`` mit einem Satz -- nie eine 0, die aussieht, als hätte er
gemessen. "0 neue Treffer" ist nur dann eine Zahl, wenn wirklich gesucht wurde.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import json

from ebay_tracker.models import SOURCE_LABELS, format_money

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
    count = engine.count_new_since(now() - timedelta(hours=24))
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


SHORT_VERDICT = {"top": "Top-Deal", "gut": "Gut", "fair": "Fair", "teuer": "Teuer", "verdaechtig": "Verdacht"}


def _headline(listing) -> str:
    """Eine Zeile, weil Nest von einem Listeneintrag nur title + meta zeigt."""
    a = listing.assessment or {}
    if a.get("summary"):
        return f"{listing.price_text} · {a['summary']}"
    return f"{listing.price_text} · {listing.title}"


def latest(engine: Engine, size: str | None) -> dict[str, Any]:
    if not engine.searches:
        return _no_searches()
    rows = LIST_ROWS.get(size or "", 4)
    # Alles Sichtbare, nicht nur Gemeldetes: "Fair" gehört in die Übersicht,
    # auch wenn es kein Push wert war.
    matches = engine.recent_matches(limit=rows, include_silent=True)
    if not matches:
        return {"kind": "list", "state": "empty", "title": "Neueste Treffer", "items": [],
                "message": "Noch nichts Passendes gefunden – die Suchen laufen weiter."}
    items = []
    for m in matches:
        a = m.listing.assessment or {}
        verdict = a.get("verdict")
        state = {"top": "ok", "gut": "ok", "teuer": "warn", "verdaechtig": "warn"}.get(verdict, "ok")
        items.append({"id": m.key + "@" + m.search, "title": _headline(m.listing),
                      "meta": SHORT_VERDICT.get(verdict) or ago(m.first_seen), "state": state})
    return {"kind": "list", "state": "ok", "title": "Neueste Treffer", "items": items}


def searches(engine: Engine, size: str | None) -> dict[str, Any]:
    if not engine.parsed:
        return _no_searches()
    states = engine.store.all_search_states()
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
        # Kurz halten: Nest zeigt detail ungekürzt rechts neben dem Namen
        detail = f"{row['last_matched']} passend"
        if row["last_error"]:
            checks.append({"label": s.name, "state": "error", "detail": "Fehler"})
            worst = "warn"
        elif row["source_errors"]:
            broken = ", ".join(SOURCE_LABELS.get(k, k) for k in json.loads(row["source_errors"]))
            checks.append({"label": s.name, "state": "warn", "detail": f"{detail} · {broken} hakt"})
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
    matches = engine.recent_matches(limit=TABLE_ROWS.get(size or "", 6), include_silent=True)
    columns = [
        {"key": "title", "label": "Angebot"},
        {"key": "price", "label": "Preis", "align": "right"},
        {"key": "verdict", "label": "Urteil"},
        {"key": "age", "label": "Gefunden", "align": "right"},
    ]
    if size == "6x2" and len(engine.searches) > 1:
        columns.insert(3, {"key": "search", "label": "Suche"})
    if not matches:
        return {"kind": "table", "state": "empty", "title": "Passende Angebote",
                "message": "Noch keine passenden Angebote.", "columns": columns, "rows": []}
    rows = []
    for m in matches:
        a = m.listing.assessment or {}
        src = "KA" if m.listing.source == "kleinanzeigen" else "eBay"
        rows.append({"title": f"{a.get('summary') or m.listing.title[:60]} ({src})",
                     "price": m.listing.price_text, "verdict": SHORT_VERDICT.get(a.get("verdict"), "–"),
                     "search": m.search, "age": ago(m.first_seen).replace("vor ", "")})
    return {"kind": "table", "state": "ok", "title": "Passende Angebote", "columns": columns, "rows": rows}


def prices(engine: Engine, size: str | None) -> dict[str, Any]:
    if not engine.searches:
        return _no_searches()
    since = now() - timedelta(days=14)
    series = []
    for s in engine.active_searches:
        points = engine.store.price_series(s.name, since)
        if len(points) >= 2:
            series.append({"name": s.name, "points": [{"t": iso(t), "v": round(v, 2)} for t, v in points]})
    if not series:
        return {"kind": "chart", "state": "empty", "label": "Preisverlauf",
                "message": "Sammelt noch – ab dem zweiten Lauf steht hier der Median-Preis je Suche.",
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
