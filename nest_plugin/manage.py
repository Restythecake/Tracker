"""Was man auf der Tracker-Seite selbst einstellt: eigene Suchen, Preisklassen, Archiv.

Geschrieben wird direkt: die Seite schickt ``POST /api/edit`` an den eigenen
Server, der ``handle`` aufruft.

Eigene Suchen liegen im Plugin-Speicher (``meta``), nicht in Nests
Einstellungen: ein Plugin kann seine Einstellungen nicht selbst schreiben.
Suchen aus Nests Einstellungen bleiben dort die Wahrheit; die Seite kann sie
pausieren, ausblenden oder als eigene Suche übernehmen (bearbeiten).
"""
from __future__ import annotations

import re
import secrets
import statistics
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Iterable

from .dsl import SOURCE_ALIASES, parse_line
from .store import Match, Store, now, parse_iso

# ------------------------------------------------------------------ Preisklassen
TIERS = ("billig", "mittel", "teuer", "verdaechtig")
TIER_LABEL = {"billig": "Billig", "mittel": "Mittel", "teuer": "Teuer", "verdaechtig": "Verdächtig"}
TIER_HINT = {
    "billig": "Unter Marktwert bzw. deutlich unter dem Mittel der Suche",
    "mittel": "Um den üblichen Preis",
    "teuer": "Über Marktwert bzw. deutlich über dem Mittel der Suche",
    "verdaechtig": "Zu billig, um wahr zu sein – Betrugssignale",
}
CHEAP, PRICEY = 0.85, 1.15  # ohne Bewertung: Verhältnis zum Median der Suche
_BY_VERDICT = {"top": "billig", "gut": "billig", "fair": "mittel", "teuer": "teuer", "verdaechtig": "verdaechtig"}


def tiers(matches: Iterable[Match]) -> dict[str, str]:
    """Preisklasse je Treffer-Schlüssel.

    Mit Bewertung (PC/GPU/RAM) entscheidet das Urteil gegen den Marktwert. Ohne
    Bewertung (z. B. "Switch 2 Zelda") der Preis gegen den Median derselben
    Suche -- das ist genau die Frage "ist das billig für das, was ich suche?".
    """
    matches = list(matches)
    prices: dict[str, list[float]] = {}
    for m in matches:
        if m.listing.price and m.listing.price > 0:
            prices.setdefault(m.search, []).append(m.listing.price)
    median = {s: statistics.median(p) for s, p in prices.items() if len(p) >= 3}
    out: dict[str, str] = {}
    for m in matches:
        verdict = (m.listing.assessment or {}).get("verdict")
        if verdict in _BY_VERDICT:
            tier = _BY_VERDICT[verdict]
        else:
            mid, price = median.get(m.search), m.listing.price
            if not mid or not price:
                tier = "mittel"
            elif price <= mid * CHEAP:
                tier = "billig"
            elif price >= mid * PRICEY:
                tier = "teuer"
            else:
                tier = "mittel"
        # Dieselbe Anzeige in zwei Suchen: die günstigere Einordnung gewinnt.
        prev = out.get(m.key)
        if prev is None or TIERS.index(tier) < TIERS.index(prev):
            out[m.key] = tier
    return out


# ------------------------------------------------------------------ Archiv-Regeln
DEFAULT_RULES: dict[str, Any] = {
    "enabled": True,
    "max_age_days": 14,      # älter als das -> Archiv
    "expensive_hours": 48,   # "teuer" so lange sichtbar, dann Archiv
    "gone_hours": 48,        # so lange nicht mehr gesehen (verkauft?) -> Archiv
    "mute_archived": True,   # archivierte Anzeigen melden nie wieder
}
RULE_LIMITS = {"max_age_days": (1, 90), "expensive_hours": (0, 720), "gone_hours": (6, 720)}


def rules(store: Store) -> dict[str, Any]:
    raw = store.get_meta("archive_rules") or {}
    out = dict(DEFAULT_RULES)
    for k, v in raw.items():
        if k in out:
            out[k] = v
    return out


def auto_archive(store: Store, matches: list[Match], states: dict[str, Any]) -> int:
    """Räumt die Liste auf, damit sie nicht nervt. Nichts wird gelöscht -- nur archiviert.

    ``matches``: sichtbare Treffer; ``states``: Suchzustände (last_ok je Suche).
    Kommt eine wegen "nicht mehr gesehen" archivierte Anzeige wieder, holt der
    Tracker sie selbst zurück (siehe ``Store.revive_seen``).
    """
    r = rules(store)
    if not r["enabled"]:
        return 0
    t = now()
    tier_of = tiers(matches)
    todo: dict[str, str] = {}
    for m in matches:
        l, first = m.listing, m.first_seen
        age = t - first if first else timedelta(0)
        if age > timedelta(days=int(r["max_age_days"])):
            todo.setdefault(m.key, f"älter als {r['max_age_days']} Tage")
            continue
        hours = int(r["expensive_hours"])
        if hours and tier_of.get(m.key) == "teuer" and age > timedelta(hours=hours):
            todo.setdefault(m.key, "teuer")
            continue
        seen = parse_iso(store.last_seen(m.key))
        row = states.get(m.search)
        last_ok = parse_iso(row["last_ok"]) if row is not None and row["last_ok"] else None
        if (seen and last_ok and last_ok > seen
                and t - seen > timedelta(hours=int(r["gone_hours"]))):
            todo.setdefault(m.key, "nicht mehr online")
    by_reason: dict[str, list[str]] = {}
    for key, reason in todo.items():
        by_reason.setdefault(reason, []).append(key)
    return sum(store.archive(keys, reason=reason, auto=True) for reason, keys in by_reason.items())


# ------------------------------------------------------------------ eigene Suchen
@dataclass
class CustomSearch:
    id: str
    line: str


def custom_searches(store: Store) -> list[CustomSearch]:
    return [CustomSearch(str(c["id"]), str(c["line"])) for c in store.get_meta("custom_searches") or []
            if isinstance(c, dict) and c.get("id") and c.get("line")]


def overrides(store: Store) -> dict[str, str]:
    """Suchen aus Nests Einstellungen, die auf der Seite pausiert ('paused') oder
    ausgeblendet/übernommen ('hidden') wurden -- nach Name."""
    raw = store.get_meta("search_overrides") or {}
    return {str(k): str(v) for k, v in raw.items() if v in ("paused", "hidden")}


def _split(value: Any) -> list[str]:
    """Liste aus Formularwert: Liste oder kommagetrennter Text."""
    if isinstance(value, list):
        items = value
    else:
        items = re.split(r"[,\n]", str(value or ""))
    return [str(x).strip() for x in items if str(x).strip()]


def _term(prefix: str, term: str) -> str:
    term = term.replace('"', "")
    return f'{prefix}"{term}"' if " " in term else f"{prefix}{term}"


def _price(value: Any) -> str | None:
    text = str(value if value is not None else "").strip().replace("€", "").replace(",", ".")
    if not text:
        return None
    number = float(text)
    if number < 0:
        raise ValueError("Preis darf nicht negativ sein")
    return f"{number:g}"


def build_line(f: dict[str, Any]) -> str:
    """Formularfelder -> eine Suchzeile in der Tracker-Syntax (siehe dsl.py)."""
    if str(f.get("line") or "").strip():  # Profi-Modus: die Zeile direkt
        return str(f["line"]).strip()
    queries = [q for q in re.split(r"\s*[|\n]\s*", str(f.get("query") or "").strip()) if q]
    if not queries:
        raise ValueError("Wonach soll gesucht werden? Suchbegriff fehlt.")
    parts: list[str] = []
    name = str(f.get("name") or "").strip().replace('"', "")
    if name:
        parts.append(f'name:"{name}"')
    parts.append(" | ".join(q.replace('"', "") for q in queries))
    lo, hi = _price(f.get("min")), _price(f.get("max"))
    if lo and hi and float(lo) > float(hi):
        raise ValueError("Mindestpreis liegt über dem Höchstpreis")
    if lo:
        parts.append(f"min:{lo}")
    if hi:
        parts.append(f"max:{hi}")
    parts += [_term("+", t) for t in _split(f.get("must"))]
    parts += [_term("~", t) for t in _split(f.get("any"))]
    parts += [_term("-", t.lstrip("-")) for t in _split(f.get("exclude"))]
    for src in _split(f.get("sources")):
        key = SOURCE_ALIASES.get(src.lower().lstrip("@"))
        if key:
            parts.append(f"@{key}")
    if f.get("condition") in ("neu", "gebraucht"):
        parts.append(f"zustand:{f['condition']}")
    if f.get("buying") in ("sofort", "auktion"):
        parts.append(f"kauf:{f['buying']}")
    evaluate = f.get("evaluate")
    if evaluate in ("pc", "gpu", "ram"):
        parts.append(f"bewertung:{evaluate}")
        deal = str(f.get("deal") or "").strip().replace(",", ".").rstrip("%")
        if deal:
            ratio = float(deal)
            ratio = ratio / 100 if ratio > 1.5 else ratio
            if not 0.2 <= ratio <= 1.5:
                raise ValueError("Deal-Schwelle zwischen 20 und 150 %")
            parts.append(f"deal:{ratio:g}")
        if str(f.get("gpu") or "").strip():
            parts.append(f"gpu:{str(f['gpu']).strip().replace(' ', '')}")
        if str(f.get("ram") or "").strip():
            parts.append(f"ram:{int(float(str(f['ram']).lower().replace('gb', '')))}")
    plz = str(f.get("plz") or "").strip()
    if plz:
        if not re.fullmatch(r"\d{5}", plz):
            raise ValueError("PLZ muss fünfstellig sein")
        parts.append(f"plz:{plz}")
    radius = str(f.get("radius") or "").strip()
    if radius:
        parts.append(f"umkreis:{int(float(radius))}")
    line = " ".join(parts)
    return ("// " + line) if f.get("paused") else line


def fields_of(line: str) -> dict[str, Any]:
    """Suchzeile -> Formularfelder (zum Bearbeiten). Unbekanntes landet im Profi-Feld."""
    p = parse_line(line)
    s = p.search
    if s is None:
        return {"line": line}
    return {
        "name": s.name, "query": " | ".join(s.all_queries),
        "min": "" if s.min_price is None else f"{s.min_price:g}",
        "max": "" if s.max_price is None else f"{s.max_price:g}",
        "must": ", ".join(s.include_all), "any": ", ".join(s.include_any), "exclude": ", ".join(s.exclude),
        "sources": list(s.sources) if "@" in line else [],
        "condition": {"new": "neu", "used": "gebraucht"}.get(s.condition, ""),
        "buying": {"fixed": "sofort", "auction": "auktion"}.get(s.buying, ""),
        "evaluate": s.evaluate or "", "deal": "" if s.min_deal is None else f"{round(s.min_deal * 100)}",
        "gpu": s.min_gpu or "", "ram": "" if s.min_ram is None else str(s.min_ram),
        "plz": s.plz or "", "radius": "" if s.radius_km is None else str(s.radius_km),
        "paused": not s.enabled, "line": "",
    }


# ------------------------------------------------------------------ die Aktion
class EditError(ValueError):
    pass


def handle(engine: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Eine Schreibaktion der Seite (``POST /api/edit``). Antwort: ``{ok, message, level}``;
    ``refresh`` nennt die Nest-Widgets, deren Daten sich dadurch ändern."""
    op = str(params.get("op") or "")
    try:
        message = _OPS[op](engine, params) if op in _OPS else None
    except (EditError, ValueError) as exc:
        return {"ok": False, "message": str(exc) or "Ungültige Eingabe", "level": "error"}
    if message is None:
        return {"ok": False, "message": f"Unbekannte Aktion '{op}'", "level": "error"}
    if isinstance(message, dict):  # Op liefert schon eine fertige Antwort (z. B. manual)
        return message
    return {"ok": True, "message": message, "level": "success", "refresh": ["gallery", "searches", "overview"]}


def _names(engine: Any) -> set[str]:
    return {s.name.lower() for s in engine.searches}


def _save(engine: Any, p: dict[str, Any]) -> str:
    store: Store = engine.store
    line = build_line(p.get("fields") or {})
    parsed = parse_line(line, engine.settings.default_sources)
    if parsed.search is None:
        raise EditError(f"Nicht verstanden: {parsed.error}")
    name = parsed.search.name
    cid = str(p.get("id") or "")
    replaces = str(p.get("replaces") or "")  # Name einer Nest-Suche, die übernommen wird
    customs = custom_searches(store)
    current = next((c for c in customs if c.id == cid), None)
    if cid and current is None:
        raise EditError("Diese Suche gibt es nicht mehr")
    old = parse_line(current.line).search if current is not None else None
    mine = {old.name.lower()} if old is not None else set()
    if replaces:
        mine.add(replaces.lower())
    taken = _names(engine) - mine
    if name.lower() in taken:
        raise EditError(f"Es gibt schon eine Suche namens „{name}“")
    if current is not None:
        current.line = line
    else:
        customs.append(CustomSearch(secrets.token_hex(4), line))
    store.set_meta("custom_searches", [c.__dict__ for c in customs])
    if replaces:
        ov = overrides(store)
        ov[replaces] = "hidden"
        store.set_meta("search_overrides", ov)
    engine.reload_searches()
    return f"„{name}“ gespeichert" + (" – läuft gleich los" if parsed.search.enabled else " (pausiert)")


def _find(engine: Any, p: dict[str, Any]) -> tuple[str, str | None]:
    """(Herkunft, eigene id) der Suche mit diesem Namen."""
    name = str(p.get("name") or "")
    for ps in engine.parsed:
        if ps.search is not None and ps.search.name == name:
            return getattr(ps, "origin", "nest"), getattr(ps, "cid", None)
    raise EditError(f"Suche „{name}“ gibt es nicht (mehr)")


def _toggle(engine: Any, p: dict[str, Any]) -> str:
    store: Store = engine.store
    origin, cid = _find(engine, p)
    name = str(p["name"])
    if origin == "custom":
        customs = custom_searches(store)
        for c in customs:
            if c.id == cid:
                paused = c.line.lstrip().startswith("//")
                c.line = c.line.lstrip()[2:].strip() if paused else "// " + c.line
                store.set_meta("custom_searches", [x.__dict__ for x in customs])
                engine.reload_searches()
                return f"„{name}“ " + ("läuft wieder" if paused else "pausiert")
        raise EditError("Suche nicht gefunden")
    ov = overrides(store)
    paused = ov.get(name) == "paused"
    if paused:
        ov.pop(name, None)
    else:
        ov[name] = "paused"
    store.set_meta("search_overrides", ov)
    engine.reload_searches()
    return f"„{name}“ " + ("läuft wieder" if paused else "pausiert")


def _delete(engine: Any, p: dict[str, Any]) -> str:
    store: Store = engine.store
    origin, cid = _find(engine, p)
    name = str(p["name"])
    if origin == "custom":
        store.set_meta("custom_searches", [c.__dict__ for c in custom_searches(store) if c.id != cid])
    else:
        ov = overrides(store)
        ov[name] = "hidden"
        store.set_meta("search_overrides", ov)
    engine.reload_searches()
    return f"„{name}“ entfernt – die Treffer bleiben im Archiv-Verlauf"


def _restore(engine: Any, p: dict[str, Any]) -> str:
    """Eine ausgeblendete Nest-Suche wieder zeigen."""
    store: Store = engine.store
    ov = overrides(store)
    name = str(p.get("name") or "")
    if ov.pop(name, None) is None:
        raise EditError("Diese Suche war nicht ausgeblendet")
    store.set_meta("search_overrides", ov)
    engine.reload_searches()
    return f"„{name}“ ist wieder da"


def _keys(p: dict[str, Any]) -> list[str]:
    keys = p.get("keys") if isinstance(p.get("keys"), list) else [p.get("key")]
    return [str(k) for k in keys if k and ":" in str(k)][:500]


def _archive(engine: Any, p: dict[str, Any]) -> str:
    keys = _keys(p)
    if not keys:
        raise EditError("Kein Angebot angegeben")
    n = engine.store.archive(keys, reason="von Hand", auto=False)
    return "Archiviert" if n == 1 else f"{n} Angebote archiviert"


def _unarchive(engine: Any, p: dict[str, Any]) -> str:
    keys = _keys(p)
    if p.get("all"):
        keys = engine.store.archived_keys()
    n = engine.store.unarchive(keys)
    return "Zurückgeholt" if n == 1 else f"{n} Angebote zurückgeholt"


def _archive_tier(engine: Any, p: dict[str, Any]) -> str:
    tier = str(p.get("tier") or "")
    if tier not in TIERS:
        raise EditError("Unbekannte Preisklasse")
    matches = engine.recent_matches(limit=2000, include_silent=True)
    keys = [k for k, t in tiers(matches).items() if t == tier]
    n = engine.store.archive(keys, reason="von Hand", auto=False)
    return f"{n} × {TIER_LABEL[tier]} archiviert"


def _rules(engine: Any, p: dict[str, Any]) -> str:
    store: Store = engine.store
    r = rules(store)
    raw = p.get("rules") or {}
    for key, (lo, hi) in RULE_LIMITS.items():
        if key in raw and str(raw[key]).strip() != "":
            value = int(float(raw[key]))
            if not lo <= value <= hi:
                raise EditError(f"{key}: {lo}–{hi}")
            r[key] = value
    for key in ("enabled", "mute_archived"):
        if key in raw:
            r[key] = bool(raw[key])
    store.set_meta("archive_rules", r)
    n = engine.tidy()
    return "Regeln gespeichert" + (f" – {n} Angebote gleich archiviert" if n else "")


def _manual(engine: Any, p: dict[str, Any]) -> dict[str, Any]:
    from . import manual
    if str(p.get("op2") or "") == "clear":
        manual.reset(engine)
        return {"ok": True, "message": "Manuelle Suche verworfen", "level": "success",
                "refresh": ["gallery", "searches", "overview"]}
    return manual.start(engine, str(p.get("line") or ""), sources=p.get("sources") or None)


def _manual_save(engine: Any, p: dict[str, Any]) -> str:
    """„Als Suche speichern": die Zeile der letzten Manuellen Suche wird echt."""
    line = str(p.get("line") or "")
    if not line:
        raise EditError("Kein Suchergebnis zum Speichern")
    return _save(engine, {"fields": {"line": line}})


_OPS = {
    "search-save": _save, "search-toggle": _toggle, "search-delete": _delete, "search-restore": _restore,
    "archive": _archive, "unarchive": _unarchive, "archive-tier": _archive_tier, "rules": _rules,
    "manual": _manual, "manual-save": _manual_save,
}
