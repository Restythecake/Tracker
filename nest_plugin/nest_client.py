"""Plugin -> Nest: der Rückkanal über /api/plugin/v1 mit dem Plugin-Token.

Nur zwei Aufrufe werden gebraucht: ``/self`` (die echten Einstellungen) und
``/notify``. Alles andere, was Nest anbietet, braucht dieser Tracker nicht --
und jede zusätzliche Fähigkeit wäre ein Scope, den der Betreiber freigeben muss.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


class NestError(RuntimeError):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code


@dataclass
class NotifyResult:
    ok: bool
    suppressed: bool = False
    error: str | None = None


class NestClient:
    def __init__(self, base_url: str | None = None, token: str | None = None, timeout: float = 8):
        self.base_url = (base_url or os.environ.get("NEST_PLUGIN_URL") or "http://nest:8080/api/plugin/v1").rstrip("/")
        self.token = token if token is not None else os.environ.get("NEST_PLUGIN_TOKEN", "")
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def _call(self, method: str, path: str, body: Any = None) -> Any:
        if not self.token:
            raise NestError(0, "no_token", "NEST_PLUGIN_TOKEN ist nicht gesetzt")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read() or b"{}").get("data")
        except urllib.error.HTTPError as exc:
            try:
                err = json.loads(exc.read()).get("error", {})
            except ValueError:
                err = {}
            raise NestError(exc.code, err.get("code", "http_error"), err.get("message", exc.reason)) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise NestError(0, "unreachable", str(getattr(exc, "reason", exc))) from None

    def self_view(self) -> dict[str, Any]:
        return self._call("GET", "/self") or {}

    def notify(self, title: str, body: str = "", level: str = "default", url: str | None = None) -> NotifyResult:
        payload: dict[str, Any] = {"title": title[:256], "body": body[:4096], "level": level}
        if url:
            # Nest ignoriert unbekannte Felder (PLUGIN-API §5.5). Ein Nest, das
            # "url" kennt, macht daraus den Klick-Link der Push-Nachricht;
            # ein älteres verwirft ihn und die URL steht trotzdem im Text.
            payload["url"] = url
        try:
            data = self._call("POST", "/notify", payload) or {}
            return NotifyResult(ok=bool(data.get("ok", True)), suppressed=bool(data.get("suppressed")))
        except NestError as exc:
            log.warning("Benachrichtigung fehlgeschlagen: %s", exc)
            return NotifyResult(ok=False, error=str(exc))
