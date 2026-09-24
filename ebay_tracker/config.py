from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

MARKETPLACES = {
    "EBAY_DE": ("www.ebay.de", "EUR"),
    "EBAY_AT": ("www.ebay.at", "EUR"),
    "EBAY_CH": ("www.ebay.ch", "CHF"),
    "EBAY_US": ("www.ebay.com", "USD"),
    "EBAY_GB": ("www.ebay.co.uk", "GBP"),
    "EBAY_FR": ("www.ebay.fr", "EUR"),
    "EBAY_IT": ("www.ebay.it", "EUR"),
    "EBAY_ES": ("www.ebay.es", "EUR"),
    "EBAY_NL": ("www.ebay.nl", "EUR"),
}

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(ValueError):
    pass


@dataclass
class Search:
    name: str
    query: str
    min_price: float | None = None
    max_price: float | None = None
    include_all: list[str] = field(default_factory=list)
    include_any: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    condition: str = "any"  # any | new | used
    buying: str = "all"  # all | fixed | auction
    category_id: str | None = None
    enabled: bool = True


@dataclass
class Settings:
    marketplace: str = "EBAY_DE"
    source: str = "auto"  # auto | api | scraper
    interval_minutes: float = 5
    database: str = "data/seen.db"
    notify_on_first_run: bool = False
    max_results: int = 50
    ebay_client_id: str | None = None
    ebay_client_secret: str | None = None

    @property
    def domain(self) -> str:
        return MARKETPLACES[self.marketplace][0]

    @property
    def currency(self) -> str:
        return MARKETPLACES[self.marketplace][1]


@dataclass
class Config:
    settings: Settings
    searches: list[Search]
    notifiers: dict[str, Any]


def _expand_env(value: Any) -> Any:
    """Ersetzt ${VAR} durch Umgebungsvariablen. Nicht gesetzte Variablen werden zu None."""
    if isinstance(value, str):
        match = _ENV_PATTERN.fullmatch(value.strip())
        if match:
            return os.environ.get(match.group(1)) or None
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def _str_list(value: Any, field_name: str, search_name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value]
    raise ConfigError(f"Suche '{search_name}': '{field_name}' muss eine Liste sein")


def _parse_search(raw: dict[str, Any]) -> Search:
    name = str(raw.get("name") or raw.get("query") or "").strip()
    query = str(raw.get("query") or "").strip()
    if not query:
        raise ConfigError(f"Suche '{name or '?'}' hat kein 'query'")

    condition = str(raw.get("condition", "any")).lower()
    if condition not in {"any", "new", "used"}:
        raise ConfigError(f"Suche '{name}': condition muss any/new/used sein")
    buying = str(raw.get("buying", "all")).lower()
    if buying not in {"all", "fixed", "auction"}:
        raise ConfigError(f"Suche '{name}': buying muss all/fixed/auction sein")

    min_price = raw.get("min_price")
    max_price = raw.get("max_price")
    if min_price is not None and max_price is not None and float(min_price) > float(max_price):
        raise ConfigError(f"Suche '{name}': min_price ist größer als max_price")

    return Search(
        name=name,
        query=query,
        min_price=float(min_price) if min_price is not None else None,
        max_price=float(max_price) if max_price is not None else None,
        include_all=_str_list(raw.get("include_all"), "include_all", name),
        include_any=_str_list(raw.get("include_any"), "include_any", name),
        exclude=_str_list(raw.get("exclude"), "exclude", name),
        condition=condition,
        buying=buying,
        category_id=str(raw["category_id"]) if raw.get("category_id") else None,
        enabled=bool(raw.get("enabled", True)),
    )


def load_config(path: str | Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Konfigurationsdatei nicht gefunden: {path}")
    with path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    raw = _expand_env(raw)

    raw_settings = raw.get("settings") or {}
    settings = Settings(
        marketplace=str(raw_settings.get("marketplace", "EBAY_DE")).upper(),
        source=str(raw_settings.get("source", "auto")).lower(),
        interval_minutes=float(raw_settings.get("interval_minutes", 5)),
        database=str(raw_settings.get("database", "data/seen.db")),
        notify_on_first_run=bool(raw_settings.get("notify_on_first_run", False)),
        max_results=int(raw_settings.get("max_results", 50)),
        ebay_client_id=raw_settings.get("ebay_client_id") or os.environ.get("EBAY_CLIENT_ID"),
        ebay_client_secret=raw_settings.get("ebay_client_secret") or os.environ.get("EBAY_CLIENT_SECRET"),
    )
    if settings.marketplace not in MARKETPLACES:
        raise ConfigError(f"Unbekannter Marktplatz '{settings.marketplace}'. Erlaubt: {', '.join(MARKETPLACES)}")
    if settings.source not in {"auto", "api", "scraper"}:
        raise ConfigError("settings.source muss auto/api/scraper sein")
    if settings.interval_minutes < 1:
        raise ConfigError("settings.interval_minutes muss mindestens 1 sein (sonst blockt eBay)")

    searches = [_parse_search(s) for s in raw.get("searches") or []]
    names = [s.name for s in searches]
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        raise ConfigError(f"Doppelte Suchnamen: {', '.join(sorted(duplicates))}")
    if not any(s.enabled for s in searches):
        raise ConfigError("Keine aktive Suche in der Konfiguration")

    return Config(settings=settings, searches=searches, notifiers=raw.get("notifiers") or {})
