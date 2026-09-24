"""Gemeinsamer HTTP-Client der Scraper.

eBay sitzt hinter Akamai Bot Manager. Der bewertet u. a. den TLS-Fingerprint:
Python-``requests`` (OpenSSL) bekam in Messungen vom 24.09.2026 bei jedem
dritten Aufruf ein 403, dieselben Header über curl oder ``curl_cffi`` mit
Chrome-Impersonation 6 von 6 Mal ein 200. Deshalb ist ``curl_cffi`` der
Standard; fehlt es, geht es mit ``requests`` weiter (schlechter, aber lauffähig).
"""
from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)

try:  # pragma: no cover - hängt von der Installation ab
    from curl_cffi import requests as _cffi
    from curl_cffi.requests.exceptions import RequestException as _CffiError

    HAS_CFFI = True
except Exception:  # ImportError, aber auch eine kaputte native Bibliothek
    _cffi = None
    _CffiError = requests.RequestException
    HAS_CFFI = False

# Beide Fehlerfamilien, damit Aufrufer nur eine Stelle kennen müssen.
REQUEST_ERRORS: tuple[type[BaseException], ...] = (requests.RequestException, _CffiError)

FALLBACK_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/18.0 Safari/605.1.15"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Dest": "document",
}


def new_session(accept_language: str = "de-DE,de;q=0.9,en;q=0.8"):
    """Eine Browser-ähnliche Session. Mit curl_cffi setzt die Impersonation
    User-Agent und Co. passend zum TLS-Fingerprint; eigene Werte würden den
    Fingerprint verraten, also kommt nur die Sprache dazu."""
    if HAS_CFFI:
        session = _cffi.Session(impersonate="chrome")
        session.headers.update({"Accept-Language": accept_language})
        return session
    session = requests.Session()
    session.headers.update(FALLBACK_HEADERS)
    session.headers["Accept-Language"] = accept_language
    return session
