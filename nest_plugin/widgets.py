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
from ebay_tracker.sources.fetch import STATS

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
                      "meta": SHORT_VERDICT.get(verdict) or ago(m.first_seen), "state": state,
                      "url": m.listing.url})
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
    checks += source_checks()
    nxt = until(engine.next_run_at)
    label = "Läuft gerade …" if engine.running else (f"Nächste Prüfung {nxt}" if nxt else "Suchen")
    return {"kind": "status", "state": worst, "label": label, "checks": checks,
            **({"message": _warning(engine)} if worst == "warn" and _warning(engine) else {})}


def source_checks() -> list[dict[str, Any]]:
    """Je Quelle seit dem Start: "eBay: 12 ok · 5 per Browser gerettet · 0 gesperrt"."""
    rows = []
    for name, s in sorted(STATS.snapshot().items(), key=lambda kv: list(SOURCE_LABELS).index(kv[0])
                          if kv[0] in SOURCE_LABELS else 99):
        if not s.requests:
            continue
        detail = f"{s.http_ok + s.browser_ok} ok · {s.rescued} per Browser gerettet · {s.blocked} gesperrt"
        rows.append({"label": SOURCE_LABELS.get(name, name), "detail": detail,
                     "state": "ok" if not s.blocked else ("warn" if s.ok else "error")})
    return rows


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


CARD_TONE = {"top": "ok", "gut": "ok", "fair": "info", "teuer": "warn", "verdaechtig": "error"}


def image_path(key: str) -> str:
    """Nest erlaubt in Asset-Pfaden nur [A-Za-z0-9/._-~+=] -- "ebay:123" -> "/img/ebay-123"."""
    return "/img/" + key.replace(":", "-", 1)


def key_from_image(path: str) -> str:
    return path.replace("-", ":", 1)


def offers(engine: Engine, size: str | None) -> dict[str, Any]:
    """Native Kartenleiste (Nest-Widget-Art ``cards``): Bild, Preis, Urteil, Link."""
    if not engine.searches:
        return _no_searches()
    # Deals zuerst, darin der beste Preis zuerst; "teuer" ganz hinten
    rank = {"top": 0, "verdaechtig": 1, "gut": 2, "fair": 3, "teuer": 4}
    pool = engine.recent_matches(limit=60, include_silent=True)
    pool.sort(key=lambda m: (rank.get((m.listing.assessment or {}).get("verdict"), 3),
                             (m.listing.assessment or {}).get("ratio") or 9))
    matches = pool[:12]
    if not matches:
        return {"kind": "cards", "state": "empty", "items": [],
                "message": "Noch keine passenden Angebote – die Suchen laufen."}
    items = []
    for m in matches:
        l = m.listing
        a = l.assessment or {}
        # Karten sind schmal: nur der Betrag als Preis, Zusätze in die Unterzeile
        price = format_money(l.price, l.currency) if l.price else (l.price_note or "Preis ?")
        extra = "VB" if l.price_note == "VB" else ("inkl. KS" if l.price_note == "inkl. Käuferschutz" else None)
        item = {"id": m.key + "@" + m.search, "title": a.get("summary") or l.title,
                "subtitle": " · ".join(x for x in (l.source_label, extra, l.location) if x),
                "price": price, "meta": ago(m.first_seen), "url": l.url}
        if l.image_url:
            item["image"] = image_path(m.key)
        if a.get("verdict"):
            item["badge"] = SHORT_VERDICT.get(a["verdict"], a["verdict"])
            item["badgeTone"] = CARD_TONE.get(a["verdict"], "info")
        items.append(item)
    return {"kind": "cards", "state": "ok", "items": items}


def restock(engine: Engine, size: str | None) -> dict[str, Any]:
    """Shop-Verfügbarkeit der Zelda-Edition (und was sonst beobachtet wird)."""
    rows = engine.store.restock_rows()
    if not rows:
        return {"kind": "status", "state": "loading", "label": "Shops",
                "message": "Erste Shop-Prüfung läuft …"}
    checks = []
    worst = "ok"
    any_unknown = False
    for r in rows:
        av = r["available"]
        if av is None:
            state, detail = "empty", (r["note"] or "unlesbar")
            any_unknown = True
        elif av:
            state, detail = "ok", f"VERFÜGBAR · {int(r['price']) if r['price'] else '?'} €"
            worst = "warn"
        else:
            state, detail = "empty", f"ausverkauft · {int(r['price'])} €" if r["price"] else "ausverkauft"
        changed = parse_iso(r["changed_at"])
        if changed and (now() - changed).total_seconds() < 6 * 3600:
            detail += f" · geändert {ago(changed)}"
        checks.append({"label": r["shop"], "state": state, "detail": detail[:120]})
    if any_unknown:
        worst = "warn"
    payload = {"kind": "status", "state": worst, "label": "Zelda-Edition in Shops", "checks": checks}
    return payload


WIDGETS = {
    "overview": overview,
    "latest": latest,
    "searches": searches,
    "deals": deals,
    "prices": prices,
    "restock": restock,
    "offers": offers,
}
