"""Manuelle Suche: einmalig über alle Quellen, im Hintergrund.

Der Unterschied zu einer normalen Suche: sie wird *nicht* dauerhaft beobachtet
und meldet nichts -- sie sammelt einmal alle Treffer, die sie gerade sieht.
Ergebnisse leben im Plugin-Speicher (``meta``), bis sie überschrieben oder
gelöscht werden; „Als Suche speichern" macht daraus eine echte.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from ebay_tracker.config import Search
from ebay_tracker.filters import rejection_reason
from ebay_tracker.models import Listing

from .dsl import parse_line
from .store import iso, now

KEY = "manual"          # meta-Schlüssel
MAX_KEEP = 300          # so viele Treffer werden behalten
HISTORY = 5             # so viele ältere Suchen bleiben aufrufbar


@dataclass
class ManualRun:
    id: str
    line: str
    name: str
    query: str
    status: str                     # running | done | error
    started_at: str
    finished_at: str | None = None
    per_source: dict[str, str] = field(default_factory=dict)   # Quelle -> "12 Treffer" oder Fehler
    fetched: int = 0
    kept: int = 0
    error: str | None = None
    keys: list[str] = field(default_factory=list)  # Anzeige-Schlüssel in Reihenfolge


def _sources_of(engine: Any, wanted: list[str] | None) -> list[str]:
    known = list(engine.source.sources) if engine.source else []
    if not wanted:
        return known
    return [s for s in wanted if s in known]


def state_of(engine: Any) -> dict[str, Any]:
    st = engine.store.get_meta(KEY)
    return st if isinstance(st, dict) else {}


def _save_state(engine: Any, st: dict[str, Any]) -> None:
    engine.store.set_meta(KEY, st)


def start(engine: Any, line: str, *, sources: list[str] | None = None) -> dict[str, Any]:
    """Eine Manuelle Suche starten. Läuft in eigenem Thread, Antwort kommt sofort."""
    line = (line or "").strip().replace("\n", " ")
    if not line:
        return {"ok": False, "message": "Wonach soll gesucht werden?", "level": "error"}
    # Manuelle Suchen sind immer enabled und ohne Meldung: der Punkt ist "was gibt es
    # gerade", nicht "melde mir ab jetzt". Bewusst ohne bewertung:? -- doch, mit: das
    # Formular erlaubt alles, was eine normale Suche kann.
    parsed = parse_line(line, engine.settings.default_sources)
    if parsed.search is None:
        return {"ok": False, "message": f"Nicht verstanden: {parsed.error}", "level": "error"}
    s = parsed.search
    s.enabled = True
    s.sources = _sources_of(engine, s.sources) or list(engine.settings.default_sources)
    if not s.sources:
        return {"ok": False, "message": "Keine Quelle eingerichtet", "level": "error"}

    run = ManualRun(id=iso(now()), line=line, name=s.name, query=s.query, status="running",
                    started_at=iso(now()))
    st = state_of(engine)
    if (st.get("current") or {}).get("status") == "running":
        return {"ok": False, "message": "Eine manuelle Suche läuft schon", "level": "warning"}
    st["current"] = run.__dict__  # schon beim Start: die Seite sieht "läuft" sofort
    _save_state(engine, st)
    thread = threading.Thread(target=_work, args=(engine, s, run), daemon=True,
                              name=f"manual-{s.name}")
    thread.start()
    return {"ok": True, "message": f"Suche „{s.name}“ läuft über {len(s.sources)} Quellen",
            "level": "success"}


def _work(engine: Any, s: Search, run: ManualRun) -> None:
    """Der Suchende. Schreibt bei Quelle fertig ihren Stand ins meta -- die Seite
    kann zwischendrin nachsehen, ohne zu warten."""
    store = engine.store
    try:
        raw, errors = engine.source.search_detailed(s)
    except Exception as exc:  # defensively; MultiSource fängt Quellfehler selbst
        raw, errors = [], {"fehler": str(exc)}
    # Wie in _check: Ausschlussbegriffe/Preisgrenzen nochmal anwenden (der
    # Marktplatz-Parser filtert das nicht immer zuverlässig vor).
    kept: list[Listing] = []
    for l in raw:
        if rejection_reason(l, s) is None and l.price is not None and l.price > 0:
            kept.append(l)
    # Details für GPU-PCs nachladen wäre zu teuer für einen Klick; die Karten
    # zeigen Titel, Preis, Ort -- und /go/ führt hin.
    kept.sort(key=lambda l: (l.price or 0), reverse=False)
    by_source: dict[str, list[Listing]] = {}
    for l in kept:
        by_source.setdefault(l.source, []).append(l)
    # Bilder vorwärmen: die Seite lädt sie ohnehin; der Cache speichert sie
    for l in kept[:60]:
        store.upsert_listing(l)
    keys = [l.key for l in kept[:MAX_KEEP]]
    run.per_source = {**{k: f"{len(v)} gefunden" for k, v in by_source.items()},
                      **{f"{k} (Fehler)": v for k, v in errors.items()}}
    run.fetched = len(raw)
    run.kept = len(keys)
    run.status, run.finished_at = "done", iso(now())
    _finish(engine, run, s, keys)


def _finish(engine: Any, run: ManualRun, s: Search, keys: list[str]) -> None:
    st = state_of(engine)
    prev = st.get("history") or []
    st.update({"current": run.__dict__, "results": keys,
               "line": run.line, "query": s.query, "name": s.name,
               "history": ([run.__dict__] + [h for h in prev
                            if h.get("line") != run.line])[:HISTORY]})
    _save_state(engine, st)


def _state_now(engine: Any) -> dict[str, Any]:
    """Zustand für die Seite (ohne Thread-Block)."""
    return state_of(engine)


def results_of(engine: Any, st: dict[str, Any] | None = None) -> list[Any]:
    """Die Listing-Objekte der laufenden/letzten Suche, in der gespeicherten Reihenfolge."""
    st = st or state_of(engine)
    out = []
    for key in st.get("results") or []:
        if (l := engine.store.listing(key)) is not None:
            out.append(l)
    return out


def reset(engine: Any) -> None:
    """Ergebnisse verwerfen (z. B. „Verwerfen"-Knopf auf der Suche-Seite)."""
    st = state_of(engine)
    _save_state(engine, {"history": st.get("history") or []})
