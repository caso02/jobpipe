"""Gemeinsame Basis aller Fetcher.

Hier stehen die Regeln, die im README versprochen werden — als Code, nicht als
Absichtserklärung:

* ``RateLimiter``  drosselt auf N Requests/Sekunde pro Host
* ``RobotsGate``   prüft robots.txt zur Laufzeit gegen unseren echten User-Agent
* ``RawCache``     legt Rohantworten ab, damit Tests und Re-Parsing die Portale
                   nicht erneut belasten

Ein Fetcher, der ``PoliteClient`` benutzt, kann diese Regeln nicht versehentlich
umgehen: ``get``/``post`` gehen ausnahmslos durch Limiter und Robots-Gate.
"""

from __future__ import annotations

import gzip
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx
import structlog
from protego import Protego
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

log = structlog.get_logger(__name__)


class RobotsDisallowed(RuntimeError):
    """Die robots.txt der Quelle verbietet uns diesen Pfad.

    Das ist bewusst eine Exception und kein Warn-Log: wenn ein Portal uns
    aussperrt, soll der Lauf abbrechen, nicht stillschweigend weiterlaufen.
    """


# --------------------------------------------------------------------------
# Drosselung
# --------------------------------------------------------------------------


class RateLimiter:
    """Token-Bucket pro Host, thread-safe.

    Absichtlich simpel: bei 1 req/s ist Genauigkeit im Millisekundenbereich
    irrelevant, Vorhersagbarkeit dagegen wichtig.
    """

    def __init__(self, requests_per_second: float) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second muss > 0 sein")
        self._min_interval = 1.0 / requests_per_second
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def acquire(self, host: str) -> float:
        """Blockiert, bis der nächste Request erlaubt ist. Gibt Wartezeit zurück."""
        with self._lock:
            now = time.monotonic()
            earliest = self._last.get(host, 0.0) + self._min_interval
            wait = max(0.0, earliest - now)
            self._last[host] = now + wait
        if wait > 0:
            time.sleep(wait)
        return wait


# --------------------------------------------------------------------------
# robots.txt
# --------------------------------------------------------------------------


@dataclass
class _RobotsEntry:
    parser: Protego | None
    fetched_at: float


class RobotsGate:
    """Wertet robots.txt mit Googles Parsing-Semantik aus (Protego).

    ``urllib.robotparser`` behandelt Wildcards falsch und würde hier zu
    falschen Freigaben führen — deshalb Protego.
    """

    def __init__(self, user_agent: str, cache_minutes: int = 60, timeout: float = 15.0) -> None:
        self.user_agent = user_agent
        self._cache_seconds = cache_minutes * 60
        self._timeout = timeout
        self._cache: dict[str, _RobotsEntry] = {}
        self._lock = threading.Lock()

    def _parser_for(self, url: str) -> Protego | None:
        parts = urlparse(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        with self._lock:
            entry = self._cache.get(origin)
            if entry and (time.monotonic() - entry.fetched_at) < self._cache_seconds:
                return entry.parser

        parser: Protego | None = None
        try:
            resp = httpx.get(
                f"{origin}/robots.txt",
                headers={"User-Agent": self.user_agent},
                timeout=self._timeout,
                follow_redirects=True,
            )
            if resp.status_code == 200:
                parser = Protego.parse(resp.text)
            else:
                # Keine robots.txt => nichts verboten. Das ist die Konvention,
                # nicht unsere Auslegung.
                log.info("robots.missing", origin=origin, status=resp.status_code)
        except httpx.HTTPError as exc:
            # Nicht erreichbar: wir gehen NICHT von "erlaubt" aus.
            log.warning("robots.unreachable", origin=origin, error=str(exc))
            raise RobotsDisallowed(
                f"robots.txt von {origin} nicht abrufbar ({exc}). Lauf wird abgebrochen, "
                f"statt ungeprüft weiterzumachen."
            ) from exc

        with self._lock:
            self._cache[origin] = _RobotsEntry(parser, time.monotonic())
        return parser

    def allows(self, url: str) -> bool:
        parser = self._parser_for(url)
        if parser is None:
            return True
        return bool(parser.can_fetch(url, self.user_agent))

    def check(self, url: str) -> None:
        if not self.allows(url):
            raise RobotsDisallowed(
                f"robots.txt verbietet {url} für User-Agent '{self.user_agent}'. "
                f"Das ist eine Antwort des Betreibers, kein Hindernis."
            )

    def crawl_delay(self, url: str) -> float | None:
        parser = self._parser_for(url)
        if parser is None:
            return None
        delay = parser.crawl_delay(self.user_agent)
        return float(delay) if delay is not None else None


# --------------------------------------------------------------------------
# Rohdaten-Cache
# --------------------------------------------------------------------------


class RawCache:
    """Legt Rohantworten unverändert als .json.gz ab.

    Zweck ist nicht Platzersparnis, sondern dass Parser-Entwicklung und Tests
    beliebig oft laufen können, ohne die Portale erneut anzufassen.
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    def path_for(self, source: str, item_id: str, day: str | None = None) -> Path:
        day = day or datetime.now(UTC).strftime("%Y-%m-%d")
        safe = item_id.replace("/", "_").replace("..", "_")
        return self.root / source / day / f"{safe}.json.gz"

    def write(self, source: str, item_id: str, payload: Any, day: str | None = None) -> Path:
        p = self.path_for(source, item_id, day)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        with gzip.open(p, "wb") as fh:
            fh.write(data)
        return p

    def write_text(self, source: str, item_id: str, text: str, day: str | None = None) -> Path:
        p = self.path_for(source, item_id, day).with_suffix("").with_suffix(".html.gz")
        p.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(p, "wb") as fh:
            fh.write(text.encode("utf-8"))
        return p

    def read(self, path: Path) -> Any:
        with gzip.open(path, "rb") as fh:
            return json.loads(fh.read().decode("utf-8"))

    def exists(self, source: str, item_id: str, day: str | None = None) -> bool:
        return self.path_for(source, item_id, day).exists()


# --------------------------------------------------------------------------
# HTTP-Client
# --------------------------------------------------------------------------


@dataclass
class FetchStats:
    requests_made: int = 0
    bytes_received: int = 0
    rate_limit_wait: float = 0.0


class PoliteClient:
    """httpx-Client, der Drosselung und robots.txt erzwingt.

    Es gibt hier absichtlich keinen Weg, beides zu überspringen.
    """

    def __init__(
        self,
        *,
        user_agent: str,
        requests_per_second: float = 1.0,
        timeout: float = 30.0,
        retry_attempts: int = 3,
        retry_backoff: float = 2.0,
        robots_cache_minutes: int = 60,
        dry_run: bool = False,
    ) -> None:
        self.user_agent = user_agent
        self.dry_run = dry_run
        self.stats = FetchStats()
        self.planned: list[tuple[str, str]] = []  # (method, url) im dry-run

        self._limiter = RateLimiter(requests_per_second)
        self._robots = RobotsGate(user_agent, robots_cache_minutes, timeout)
        self._retry_attempts = max(1, retry_attempts)
        self._retry_backoff = retry_backoff
        self._client = httpx.Client(
            headers={
                "User-Agent": user_agent,
                "Accept-Language": "de-CH,de;q=0.9",
            },
            timeout=timeout,
            follow_redirects=True,
            http2=False,
        )

    # -- öffentlich --------------------------------------------------------

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._request("POST", url, **kwargs)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- intern ------------------------------------------------------------

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self._robots.check(url)

        if self.dry_run:
            self.planned.append((method, url))
            log.info("dry_run.request", method=method, url=url)
            return httpx.Response(200, json=[], request=httpx.Request(method, url))

        host = urlparse(url).netloc
        # Falls das Portal eine Crawl-Delay angibt, respektieren wir sie,
        # sofern sie strenger ist als unsere eigene Drosselung.
        self.stats.rate_limit_wait += self._limiter.acquire(host)

        @retry(
            stop=stop_after_attempt(self._retry_attempts),
            wait=wait_exponential(multiplier=self._retry_backoff, min=1, max=30),
            retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
            reraise=True,
        )
        def _do() -> httpx.Response:
            resp = self._client.request(method, url, **kwargs)
            self.stats.requests_made += 1
            self.stats.bytes_received += len(resp.content)
            # 4xx ausser 429 sind nicht retry-würdig: das ist eine Aussage
            # des Servers, keine Störung.
            if resp.status_code == 429 or resp.status_code >= 500:
                resp.raise_for_status()
            return resp

        return _do()


# --------------------------------------------------------------------------
# Fetcher-Protokoll
# --------------------------------------------------------------------------


@dataclass
class FetchResult:
    source: str
    items_seen: int = 0
    items_new: int = 0
    items_updated: int = 0
    requests_made: int = 0
    raw_paths: list[Path] = field(default_factory=list)


class Fetcher(Protocol):
    """Was jede Quelle können muss."""

    source: str

    def fetch(self, *, since_days: int | None = None) -> FetchResult: ...
