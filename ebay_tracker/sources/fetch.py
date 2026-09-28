"""Abgestufter Seitenabruf: erst leicht (curl_cffi), bei Sperre per Browser.

Stufe 1 ist der bisherige HTTP-Client (``http.new_session``): schnell, wenig
RAM, reicht meistens. Sperrt der Marktplatz (403/429/503, Captcha-Seite), holt
Stufe 2 dieselbe URL mit Scraplings ``StealthySession`` -- einem echten,
getarnten Chromium. Erst wenn auch das scheitert, gilt die Quelle als gesperrt
und ``MultiSource`` pausiert sie.

Gemessen am 28.09.2026 gegen eBay (Akamai): Der kalte Browser bekommt auf die
erste Seite selbst ein 403, aber dabei laufen die Sensor-Skripte und setzen die
Cookies; die nächste Anfrage kommt mit 200 und 60 Treffern durch. Deshalb
besucht Stufe 2 je Host einmal die Startseite, bevor sie die eigentliche URL
holt.

Der Browser ist teuer (Start ~3 s, ~200–300 MB RAM). Darum:

* eine Session für den ganzen Prozess, ein Tab, in einem eigenen Thread
  (Playwrights Sync-API ist an den Thread gebunden, der sie gestartet hat);
* Bilder, Fonts, Stylesheets und Werbe-Domains werden gar nicht erst geladen;
* harte Zeitgrenze je Abruf, danach wird der Browser verworfen;
* nach ``IDLE_SECONDS`` ohne Arbeit wird er geschlossen und gibt den RAM frei.

Fehlt ``scrapling`` (schlankes Image, Tests), gibt es nur Stufe 1 -- genau
das Verhalten von vorher.
"""
from __future__ import annotations

import importlib.util
import logging
import queue
import re
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlsplit

from ..config import FETCH_MODES as MODES
from .base import SourceError
from .http import REQUEST_ERRORS, new_session

log = logging.getLogger(__name__)

BLOCK_STATUS = (403, 429, 503)
# Nur auf kleinen Seiten nach Sperr-Texten suchen: eine echte Ergebnisseite hat
# über 1 MB und erwähnt "captcha" irgendwo im JavaScript.
_BLOCK_TEXT = re.compile(
    r"captcha|access denied|pardon our interruption|"
    r"ungewöhnliche aktivität|unusual traffic|bot protection",
    re.IGNORECASE,
)
# Cloudflare-Challenge: nur dann lohnt Scraplings solve_cloudflare (kostet sonst
# bei jedem Abruf ~5 s Warten auf Netzwerkruhe).
_CLOUDFLARE = re.compile(r"just a moment|cf-chl|challenges\.cloudflare\.com", re.IGNORECASE)
_SMALL_PAGE = 30_000


def blocked_reason(status: int, url: str, text: str) -> str | None:
    """Warum die Antwort eine Sperre ist -- oder None, wenn sie echt aussieht."""
    low_url = url.lower()
    if "captcha" in low_url or "splashui/challenge" in low_url:
        return "Captcha"
    if status in BLOCK_STATUS and not (len(text) < _SMALL_PAGE and _CLOUDFLARE.search(text)):
        return f"HTTP {status}"
    if len(text) < _SMALL_PAGE and _CLOUDFLARE.search(text):
        return "Cloudflare"
    if len(text) < _SMALL_PAGE and _BLOCK_TEXT.search(text):
        return "Sperrseite"
    return None


@dataclass
class Page:
    status: int
    url: str
    content: bytes
    via: str = "http"  # "http" | "browser"

    @property
    def text(self) -> str:
        # Kleinanzeigen sagt die Kodierung nicht an; requests riete Latin-1.
        return self.content.decode("utf-8", errors="replace")


# ------------------------------------------------------------------ Statistik
@dataclass
class SourceStats:
    requests: int = 0
    http_ok: int = 0
    browser_ok: int = 0  # Modus "stealth": direkt per Browser geholt
    rescued: int = 0  # Stufe 1 gesperrt, Stufe 2 erfolgreich
    blocked: int = 0  # beide Stufen gescheitert (bzw. Stufe 1 ohne Browser)
    last_rescue: float | None = None

    @property
    def ok(self) -> int:
        return self.http_ok + self.browser_ok + self.rescued


@dataclass
class FetchStats:
    since: float = field(default_factory=time.time)
    per_source: dict[str, SourceStats] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def bump(self, source: str, **counts: int) -> None:
        with self._lock:
            s = self.per_source.setdefault(source, SourceStats())
            for key, n in counts.items():
                setattr(s, key, getattr(s, key) + n)
            if counts.get("rescued"):
                s.last_rescue = time.time()

    def snapshot(self) -> dict[str, SourceStats]:
        with self._lock:
            return {k: SourceStats(**vars(v)) for k, v in self.per_source.items()}


STATS = FetchStats()


# ------------------------------------------------------------------ Stufe 2
def scrapling_installed() -> bool:
    """Browser-Fetcher da? Der Parser allein (requirements.txt) reicht nicht."""
    return all(importlib.util.find_spec(m) is not None for m in ("scrapling", "patchright"))


class Browser:
    """Eine geteilte, faul gestartete StealthySession in einem eigenen Thread."""

    IDLE_SECONDS = 300
    FETCH_TIMEOUT_MS = 30_000
    HARD_TIMEOUT = 75  # Sekunden für Aufwärmen + Abruf, dann wird verworfen
    RETRY_AFTER_FAILURE = 1800  # Browser startet nicht (fehlt?) -> so lange nur Stufe 1
    WARM_WAIT_MS = 3000  # nach Netzwerkruhe noch so lange, bis das Bot-Sensor-Skript fertig ist

    def __init__(self, session_factory=None):
        self._custom = session_factory is not None
        self._factory = session_factory or self._scrapling_session
        self._jobs: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._broken_until = 0.0
        self.starts = 0

    @property
    def available(self) -> bool:
        if time.monotonic() < self._broken_until:
            return False
        return self._custom or scrapling_installed()

    def _scrapling_session(self):
        from scrapling.fetchers import StealthySession  # lazy: fehlt im schlanken Image

        return StealthySession(
            headless=True,
            max_pages=1,
            disable_resources=True,  # Bilder, Fonts, CSS, Medien
            block_ads=True,
            locale="de-DE",
            timezone_id="Europe/Berlin",
            timeout=self.FETCH_TIMEOUT_MS,
            retries=1,  # Wiederholungen kosten bei eBay nur weitere Sperrpunkte
        )

    def fetch(self, url: str, solve_cloudflare: bool = False) -> Page:
        """Blockiert bis zum Ergebnis, höchstens ``HARD_TIMEOUT`` Sekunden."""
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._worker, name="tracker-browser", daemon=True)
                self._thread.start()
        fut: Future = Future()
        self._jobs.put((url, fut, solve_cloudflare))
        try:
            return fut.result(timeout=self.HARD_TIMEOUT)
        except FutureTimeout:
            fut.cancel()
            self._jobs.put(("__reset__", None, False))  # hängende Session nach dem Job wegwerfen
            raise SourceError("Browser-Abruf zu langsam (Zeitlimit)") from None

    def close(self) -> None:
        self._jobs.put(("__close__", None, False))

    # Alles unterhalb läuft nur im Browser-Thread.
    def _worker(self) -> None:
        session = None
        warmed: set[str] = set()

        def shutdown():
            nonlocal session
            if session is not None:
                try:
                    session.close()
                except Exception as exc:  # noqa: BLE001 - beim Aufräumen egal
                    log.debug("Browser schließen: %s", exc)
                session = None
                warmed.clear()
                log.info("Browser geschlossen (RAM frei)")

        while True:
            try:
                url, fut, cloudflare = self._jobs.get(timeout=self.IDLE_SECONDS)
            except queue.Empty:
                shutdown()
                continue
            if url in ("__reset__", "__close__"):
                shutdown()
                if url == "__close__":
                    return
                continue
            if fut.cancelled() or not fut.set_running_or_notify_cancel():
                continue
            try:
                if session is None:
                    t0 = time.monotonic()
                    session = self._factory()
                    session.start()
                    self.starts += 1
                    log.info("Browser gestartet (%.1f s)", time.monotonic() - t0)
                host = urlsplit(url).netloc

                def warm():
                    # Akamai & Co. setzen ihre Cookies beim ersten Besuch; der darf
                    # scheitern (meist 403), aber das Sensor-Skript muss zu Ende laufen.
                    # Ohne Warten (network_idle + WARM_WAIT_MS) ging die Folgeanfrage
                    # am 28.09. wieder mit 403 raus, mit Warten 200.
                    try:
                        session.fetch(f"https://{host}/", network_idle=True, wait=self.WARM_WAIT_MS)
                    except Exception as exc:  # noqa: BLE001
                        log.debug("Browser-Aufwärmen %s: %s", host, exc)
                    warmed.add(host)

                def get():
                    resp = session.fetch(url, solve_cloudflare=cloudflare) if cloudflare else session.fetch(url)
                    body = resp.body if isinstance(resp.body, bytes) else str(resp.body).encode()
                    return Page(int(resp.status), str(resp.url), body, via="browser")

                if host not in warmed:
                    warm()
                page = get()
                if blocked_reason(page.status, page.url, page.text) is not None:
                    # Cookies abgelaufen oder neu bewertet: einmal frisch aufwärmen, nochmal.
                    log.info("Browser gesperrt (%s) – wärme %s neu auf", page.status, host)
                    warm()
                    page = get()
                fut.set_result(page)
            except Exception as exc:  # noqa: BLE001 - Fehler gehen an den Aufrufer
                if session is None:  # Start gescheitert: Browser nicht installiert o. ä.
                    self._broken_until = time.monotonic() + self.RETRY_AFTER_FAILURE
                    log.warning("Browser nicht startbar, %d min nur HTTP: %s",
                                self.RETRY_AFTER_FAILURE // 60, exc)
                else:
                    shutdown()
                fut.set_exception(exc)


BROWSER = Browser()


# ------------------------------------------------------------------ Fetcher
class Fetcher:
    """Abruf für eine Quelle, mit Eskalation laut ``mode``.

    ``auto``    Stufe 1, bei Sperre Stufe 2 (Standard)
    ``http``    nur Stufe 1 -- das alte Verhalten
    ``stealth`` nur Browser (fällt auf Stufe 1 zurück, wenn keiner da ist)
    """

    STICKY_SECONDS = 3600

    def __init__(self, source: str, label: str, *, mode: str = "auto", session=None,
                 accept_language: str = "de-DE,de;q=0.9", browser: Browser | None = None,
                 stats: FetchStats | None = None):
        self.source = source
        self.label = label
        self.mode = mode if mode in MODES else "auto"
        self.accept_language = accept_language
        self._fixed_session = session is not None  # Tests reichen Fake-Sessions rein
        self.session = session if session is not None else new_session(accept_language)
        self.browser = browser if browser is not None else BROWSER
        self.stats = stats if stats is not None else STATS
        # Nach einer Rettung eine Weile direkt per Browser: jedes weitere 403 auf
        # Stufe 1 verschlechtert bei Akamai nur den Ruf unserer IP.
        self.sticky_until = 0.0

    def get(self, url: str, params: dict | None = None, timeout: float = 30) -> Page:
        """Holt eine Seite. Wirft ``SourceError(blocked=True)``, wenn alle Stufen gesperrt sind.

        Andere Statuscodes (404, 500 …) kommen als ``Page`` zurück; was sie
        bedeuten, weiß nur die Quelle."""
        if params:
            url = f"{url}{'&' if '?' in url else '?'}{urlencode(params)}"
        self.stats.bump(self.source, requests=1)
        use_browser = (self.mode == "stealth" or (self.mode == "auto" and time.monotonic() < self.sticky_until)) \
            and self.browser.available
        if not use_browser:
            page = self._http(url, timeout)
            reason = blocked_reason(page.status, page.url, page.text)
            if reason is None:
                self.stats.bump(self.source, http_ok=1)
                return page
            self._renew_session()
            if self.mode == "http" or not self.browser.available:
                self.stats.bump(self.source, blocked=1)
                raise self._blocked(reason)
            log.info("%s: Stufe 1 gesperrt (%s) – versuche Browser", self.label, reason)
            try:
                page = self._browser(url, solve_cloudflare=reason == "Cloudflare")
            except SourceError as exc:
                # Gesperrt bleibt gesperrt, auch wenn der Browser selbst hakt:
                # sonst klopfte Stufe 1 beim nächsten Lauf sofort wieder an.
                self.stats.bump(self.source, blocked=1)
                raise self._blocked(f"{reason}, Browser: {exc}") from exc
        else:
            try:
                page = self._browser(url)
            except SourceError:
                if self.mode == "stealth":
                    raise
                self.sticky_until = 0.0  # Browser hakt: zurück zu Stufe 1, beim nächsten Mal
                raise
        reason2 = blocked_reason(page.status, page.url, page.text)
        if reason2 is not None:
            self.stats.bump(self.source, blocked=1)
            raise self._blocked(reason2 if use_browser else f"{reason}, Browser: {reason2}")
        if use_browser:
            self.stats.bump(self.source, browser_ok=1)
        else:
            self.stats.bump(self.source, rescued=1)
            self.sticky_until = time.monotonic() + self.STICKY_SECONDS
            log.info("%s: per Browser gerettet (%s) – %d min direkt per Browser",
                     self.label, page.status, self.STICKY_SECONDS // 60)
        return page

    def _http(self, url: str, timeout: float) -> Page:
        try:
            resp = self.session.get(url, timeout=timeout)
        except REQUEST_ERRORS as exc:
            raise SourceError(f"{self.label} nicht erreichbar: {exc.__class__.__name__}") from exc
        return Page(resp.status_code, str(getattr(resp, "url", url)), resp.content)

    def _browser(self, url: str, solve_cloudflare: bool = False) -> Page:
        try:
            return self.browser.fetch(url, solve_cloudflare=solve_cloudflare)
        except SourceError:
            raise
        except Exception as exc:  # noqa: BLE001 - Playwright-Fehler sind vielfältig
            raise SourceError(f"{self.label}: Browser-Abruf fehlgeschlagen ({exc.__class__.__name__})") from exc

    def _renew_session(self) -> None:
        """Nach einer Sperre frische Cookies/Verbindung für den nächsten Versuch."""
        if not self._fixed_session:
            self.session = new_session(self.accept_language)

    def _blocked(self, reason: str) -> SourceError:
        return SourceError(f"{self.label} blockt die Anfrage ({reason})", blocked=True)
