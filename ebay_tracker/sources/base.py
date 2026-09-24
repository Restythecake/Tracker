from __future__ import annotations

from typing import Protocol

from ..config import Search
from ..models import Listing


class SourceError(RuntimeError):
    def __init__(self, message: str = "", *, blocked: bool = False):
        super().__init__(message)
        # True = der Marktplatz sperrt uns gerade (403/429/Captcha). Dann lohnt
        # es sich nicht, beim nächsten Lauf sofort wieder anzuklopfen.
        self.blocked = blocked


class Source(Protocol):
    def search(self, search: Search) -> list[Listing]:
        """Liefert die neuesten Angebote zu einer Suche (neueste zuerst)."""
        ...
