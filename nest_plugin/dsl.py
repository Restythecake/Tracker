"""Eine Suche als eine Zeile.

Nests Einstellungsformular kennt keine Listen von Objekten (JSON-Schema-Teilmenge,
PLUGIN-API §9) -- nur Listen von Strings, dargestellt als Tag-Eingabe. Also ist
jede Suche eine Zeile in einer kleinen Syntax, die man ohne Anleitung lesen kann:

    thinkpad t14 32gb max:600 +32gb -defekt -bastler
    name:"Gaming-PC" gaming pc rtx 4070 500-1100 +"rtx 4070" ~ryzen ~i7 -defekt @kleinanzeigen

| Token                     | Bedeutung                                        |
|---------------------------|--------------------------------------------------|
| Wörter ohne Präfix        | Suchbegriff beim Marktplatz                      |
| ``+wort`` / ``+"a b"``    | muss im Titel stehen (alle)                      |
| ``~wort``                 | mindestens eines davon muss im Titel stehen      |
| ``-wort``                 | darf nicht im Titel stehen                       |
| ``max:600`` ``<600``      | Höchstpreis                                      |
| ``min:150`` ``>150``      | Mindestpreis                                     |
| ``150-600``               | Preisspanne (``€`` erlaubt)                       |
| ``@ebay`` ``@kleinanzeigen`` (``@ka``) | nur diese Quellen (sonst alle)      |
| ``plz:10115`` ``umkreis:30`` | Kleinanzeigen-Umkreis nur für diese Suche     |
| ``zustand:neu|gebraucht`` | eBay-Zustand                                     |
| ``kauf:sofort|auktion``   | eBay-Angebotsformat                              |
| ``name:"…"``              | Anzeigename (sonst der Suchbegriff)              |
| ``a b | c d``             | mehrere Suchbegriffe, Treffer zusammengeführt    |
| ``kat:pc``                | nur Kategorie PCs                                |
| ``bewertung:pc``          | Hardware lesen, Preis bewerten, nur Deals melden |
| ``gpu:3080``              | mit Bewertung: mindestens diese Grafikkarten-Klasse |
| ``ram:32``                | mit Bewertung: mindestens so viel RAM            |
| ``//`` am Anfang          | Suche pausiert                                   |

``+re:…`` / ``-re:…`` funktionieren wie in config.yaml (regulärer Ausdruck).
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

from ebay_tracker.config import KNOWN_SOURCES, Search

SOURCE_ALIASES = {
    "ebay": "ebay",
    "kleinanzeigen": "kleinanzeigen",
    "ka": "kleinanzeigen",
    "ebay-kleinanzeigen": "kleinanzeigen",
}
_NUM = r"(\d+(?:[.,]\d+)?)\s*€?"
_RANGE = re.compile(rf"^{_NUM}\s*-\s*{_NUM}$")
_CMP = re.compile(rf"^([<>])=?{_NUM}$")
_KV = re.compile(r"^([a-zäöü]+):(.+)$", re.IGNORECASE)


@dataclass
class ParsedSearch:
    search: Search | None
    line: str
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


def _num(text: str) -> float:
    return float(text.replace(",", ".").rstrip("€").strip())


def _tokens(line: str) -> list[str]:
    try:
        return shlex.split(line, posix=True)
    except ValueError:
        # Ein offenes Anführungszeichen soll nicht die ganze Suche kosten
        return line.replace('"', " ").split()


def parse_line(line: str, default_sources: list[str] | None = None) -> ParsedSearch:
    raw = line.strip()
    result = ParsedSearch(search=None, line=raw)
    enabled = True
    if raw.startswith("//"):
        enabled = False
        raw = raw[2:].strip()
    if not raw:
        result.error = "leere Zeile"
        return result

    include_all: list[str] = []
    include_any: list[str] = []
    exclude: list[str] = []
    sources: list[str] = []
    name = None
    min_price = max_price = None
    condition, buying = "any", "all"
    plz = None
    radius = None
    category = None
    evaluate = None
    min_gpu = None
    min_ram = None
    groups: list[list[str]] = [[]]

    for tok in _tokens(raw):
        low = tok.lower()
        if tok == "|":
            groups.append([])
            continue
        if len(tok) > 1 and tok[0] in "+-~" and not _RANGE.match(tok) and not tok[1:].replace(".", "").isdigit():
            term = tok[1:]
            {"+": include_all, "-": exclude, "~": include_any}[tok[0]].append(term)
            continue
        if tok.startswith("@"):
            src = SOURCE_ALIASES.get(low[1:])
            if src:
                if src not in sources:
                    sources.append(src)
            else:
                result.warnings.append(f"unbekannte Quelle {tok}")
            continue
        if m := _RANGE.match(tok):
            min_price, max_price = _num(m.group(1)), _num(m.group(2))
            continue
        if m := _CMP.match(tok):
            if m.group(1) == "<":
                max_price = _num(m.group(2))
            else:
                min_price = _num(m.group(2))
            continue
        if m := _KV.match(tok):
            key, value = m.group(1).lower(), m.group(2).strip()
            try:
                if key in ("max", "bis"):
                    max_price = _num(value)
                elif key in ("min", "ab"):
                    min_price = _num(value)
                elif key == "name":
                    name = value
                elif key == "plz":
                    if not re.fullmatch(r"\d{5}", value):
                        raise ValueError
                    plz = value
                elif key in ("umkreis", "radius", "km"):
                    radius = int(_num(value))
                elif key == "zustand":
                    condition = {"neu": "new", "new": "new", "gebraucht": "used", "used": "used"}[value.lower()]
                elif key == "kauf":
                    buying = {"sofort": "fixed", "fixed": "fixed", "auktion": "auction", "auction": "auction"}[value.lower()]
                elif key in ("kat", "kategorie"):
                    if value.lower() not in ("pc", "pcs", "computer"):
                        raise ValueError
                    category = "pc"
                elif key in ("bewertung", "check", "profil"):
                    if value.lower() not in ("pc", "gaming-pc", "gamingpc"):
                        raise ValueError
                    evaluate = "pc"
                elif key in ("gpu", "mingpu", "grafik"):
                    from ebay_tracker.hardware import find_gpu
                    if not find_gpu(value.lower()):
                        raise ValueError
                    min_gpu = value
                elif key in ("ram", "minram"):
                    min_ram = int(_num(value.lower().replace("gb", "")))
                elif key == "re":
                    groups[-1].append(tok)  # ein nacktes re:… ist wohl als Suchwort gemeint
                else:
                    groups[-1].append(tok)
                    result.warnings.append(f"'{key}:' kenne ich nicht, als Suchwort genommen")
            except (ValueError, KeyError):
                result.warnings.append(f"'{tok}' ist ungültig und wurde ignoriert")
            continue
        groups[-1].append(tok)

    queries = [" ".join(g).strip() for g in groups if " ".join(g).strip()]
    query = queries[0] if queries else ""
    if not query:
        result.error = "kein Suchbegriff (nur Filter angegeben)"
        return result
    if evaluate and category is None:
        category = "pc"  # eine PC-Bewertung ohne PC-Kategorie wäre voller Einzelteile
    if (min_gpu or min_ram) and not evaluate:
        result.warnings.append("gpu:/ram: wirken nur zusammen mit bewertung:pc")
    if min_price is not None and max_price is not None and min_price > max_price:
        min_price, max_price = max_price, min_price
        result.warnings.append("min/max vertauscht")

    result.search = Search(
        name=(name or query)[:80],
        query=query,
        min_price=min_price,
        max_price=max_price,
        include_all=include_all,
        include_any=include_any,
        exclude=exclude,
        condition=condition,
        buying=buying,
        enabled=enabled,
        sources=sources or list(default_sources or KNOWN_SOURCES),
        plz=plz,
        radius_km=radius,
        alt_queries=queries[1:],
        category=category,
        evaluate=evaluate,
        min_gpu=min_gpu,
        min_ram=min_ram,
    )
    return result


def parse_lines(lines: list[str], default_sources: list[str] | None = None) -> list[ParsedSearch]:
    """Parst alle Zeilen; doppelte Namen bekommen ein Suffix statt zu kollidieren."""
    parsed = [parse_line(line, default_sources) for line in lines if line and line.strip()]
    seen: dict[str, int] = {}
    for p in parsed:
        if p.search is None:
            continue
        base = p.search.name
        n = seen.get(base.lower(), 0) + 1
        seen[base.lower()] = n
        if n > 1:
            p.search.name = f"{base} ({n})"
            p.warnings.append(f"Name doppelt, heißt jetzt '{p.search.name}'")
    return parsed
