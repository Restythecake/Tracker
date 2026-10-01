"""Plugin -> Nest: der Rückkanal über /api/plugin/v1 mit dem Plugin-Token.

Den Token holt sich das Plugin selbst (Plugins v2): ``POST /claim`` ohne Auth
und ohne Body, aus dem Container heraus -- Nest erkennt den Aufrufer an der
Quell-IP. Er lebt nur im Speicher; jeder Claim macht den vorherigen ungültig,
deshalb läuft der Claim unter einem Lock und nie doppelt.

Gebraucht werden sonst nur ``/self`` (die echten Einstellungen) und ``/notify``.
Jede weitere Fähigkeit wäre ein Scope, den der Betreiber freigeben muss.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger(__name__)

CLAIM_INTERVAL = 60  # Sekunden zwischen zwei Versuchen, solange Nest ablehnt


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
    def __init__(self, base_url: str | None = None, timeout: float = 8):
        self.base_url = (base_url or os.environ.get("NEST_PLUGIN_URL") or "http://nest:8080/api/plugin/v1").rstrip("/")
        self.timeout = timeout
        self.granted_scopes: list[str] = []
        self.claim_error: str | None = None  # Nests Fehlercode des letzten Claims, None = hat geklappt
        self._token = ""
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return bool(self._token)

    # ------------------------------------------------------------ transport
    def _request(self, method: str, path: str, body: Any = None, token: str = "") -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
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
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise NestError(0, "unreachable", str(getattr(exc, "reason", exc))) from None

    # ---------------------------------------------------------------- claim
    def claim(self, stale: str | None = None) -> bool:
        """Holt einen neuen Token. False heißt: Nest gibt (noch) keinen -- weiterlaufen, später wieder.

        ``stale`` ist der Token, mit dem ein Aufruf gerade 401 bekam. Hat ein
        anderer Thread inzwischen schon neu geholt, wird nicht noch einmal
        geclaimt: das würde dessen frischen Token entwerten.
        """
        with self._lock:
            if stale is not None and self._token and self._token != stale:
                return True
            try:
                data = self._request("POST", "/claim") or {}
            except NestError as exc:
                if exc.status in (401, 403, 404):
                    self._token = ""  # abgelehnt: was wir hatten, gilt nicht mehr
                if exc.code != self.claim_error:  # nur den Wechsel loggen, nicht jede Minute
                    log.warning("Nest gibt keinen Token: %s", exc)
                self.claim_error = exc.code
                return False
            token = data.get("token")
            if not isinstance(token, str) or not token:
                self.claim_error = "invalid_response"
                log.warning("Nest hat auf den Claim ohne Token geantwortet")
                return False
            self._token = token
            self.granted_scopes = [str(s) for s in data.get("grantedScopes") or []]
            self.claim_error = None
            log.info("Token von Nest geholt (Rechte: %s)", ", ".join(self.granted_scopes) or "keine")
            return True

    def keep_claimed(self, stop: threading.Event, after: Callable[[], None] | None = None,
                     interval: float = CLAIM_INTERVAL) -> None:
        """Claim beim Start und dann so lange alle ``interval`` Sekunden, wie kein Token da ist.

        Läuft für die ganze Lebenszeit: werden die Rechte in Nest entzogen und
        später wieder erteilt, holt diese Schleife den Token von selbst nach.
        ``after`` läuft nach jedem Versuch (Einstellungen holen bzw. Zustand zeigen).
        """
        while not stop.is_set():
            if not self.configured:
                self.claim()
                if after:
                    after()
            stop.wait(interval)

    # ----------------------------------------------------------------- calls
    def _call(self, method: str, path: str, body: Any = None) -> Any:
        token = self._token
        if not token:
            raise NestError(0, "no_token", "noch kein Token von Nest")
        try:
            return self._request(method, path, body, token)
        except NestError as exc:
            # 401: Token rotiert oder durch einen neueren Claim entwertet. Einmal neu holen, einmal wiederholen.
            if exc.status != 401 or not self.claim(stale=token):
                raise
        return self._request(method, path, body, self._token)

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
