from __future__ import annotations

from typing import Protocol

from ..config import Search
from ..models import Listing


class SourceError(RuntimeError):
    pass


class Source(Protocol):
    def search(self, search: Search) -> list[Listing]:
        """Liefert die neuesten Angebote zu einer Suche (neueste zuerst)."""
        ...
