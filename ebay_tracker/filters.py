"""Lokale Filterlogik: prüft, ob ein Angebot wirklich zur gewünschten Config passt."""
from __future__ import annotations

import re

from .config import Search
from .models import Listing

_NON_ALNUM = re.compile(r"[^0-9a-zäöüß]+")


def _compact(text: str) -> str:
    """'RTX-4070 Ti' -> 'rtx4070ti', damit Schreibweisen egal sind."""
    return _NON_ALNUM.sub("", text.lower())


def term_matches(term: str, title: str) -> bool:
    """Ein Begriff passt, wenn er (ohne Leer-/Sonderzeichen) im Titel vorkommt.

    Mit 're:' am Anfang wird der Rest als regulärer Ausdruck (case-insensitive)
    gegen den Originaltitel geprüft, z. B. 're:\\b4070\\b(?!\\s*ti)'.
    """
    if term.startswith("re:"):
        return re.search(term[3:], title, re.IGNORECASE) is not None
    needle = _compact(term)
    return bool(needle) and needle in _compact(title)


def rejection_reason(listing: Listing, search: Search) -> str | None:
    """Gibt None zurück, wenn das Angebot passt, sonst den Ablehnungsgrund."""
    title = listing.title

    for term in search.exclude:
        if term_matches(term, title):
            return f"enthält ausgeschlossenen Begriff '{term}'"
    for term in search.include_all:
        if not term_matches(term, title):
            return f"'{term}' fehlt"
    if search.include_any and not any(term_matches(t, title) for t in search.include_any):
        return f"keiner von {search.include_any} enthalten"

    if listing.price is not None:
        if search.min_price is not None and listing.price < search.min_price:
            return f"Preis {listing.price} < {search.min_price}"
        if search.max_price is not None and listing.price > search.max_price:
            return f"Preis {listing.price} > {search.max_price}"
    elif search.min_price is not None or search.max_price is not None:
        return "Preis unbekannt"

    if search.buying == "auction" and listing.buying_options and not listing.is_auction:
        return "keine Auktion"
    if search.buying == "fixed" and listing.buying_options and listing.buying_options == ["AUCTION"]:
        return "nur Auktion"
    return None


def matches(listing: Listing, search: Search) -> bool:
    return rejection_reason(listing, search) is None
