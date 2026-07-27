"""Fetcher für job-room.ch (SECO / arbeit.swiss).

Nutzt die öffentliche Such-API, die die job-room-Weboberfläche selbst
verwendet::

    POST /jobadservice/api/jobAdvertisements/_search?page=N&size=M&sort=date_desc

Kein Key, keine Cookies, keine Sonder-Header. robots.txt sperrt die SPA-Route
``/job-search/``, nicht ``/jobadservice/`` — geprüft wird das trotzdem bei
jedem Request über ``PoliteClient``.

Zwei Serverkonstanten bestimmen die Strategie:

* ``x-total-count`` im Response-Header gibt die Gesamttrefferzahl
* ``(page + 1) * size <= 10_000`` — darüber antwortet der Server mit HTTP 412

Deshalb das Tagesinkrement: ``onlineSince=1`` liefert für ZH/SG/TG rund 663
Inserate, also zwei Requests bei ``size=500``. Ein 30-Tage-Backfill (19'216)
würde das Fenster sprengen; ``onlineSince=7`` (6'021) passt gerade noch.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from jobpipe.collect.base import FetchResult, PoliteClient, RawCache
from jobpipe.config import Config
from jobpipe.parse.pii import scrub_job_room

log = structlog.get_logger(__name__)

SOURCE = "job_room"

#: Vom Server erzwungen. Wird geprüft, bevor eine Seite angefragt wird.
MAX_RESULT_WINDOW = 10_000


class ResultWindowExceeded(RuntimeError):
    """Die Abfrage träfe mehr als 10'000 Inserate.

    Der Server würde mit HTTP 412 antworten und selbst empfehlen, die Suche
    einzugrenzen. Wir fangen das vorher ab und sagen konkret, was zu tun ist.
    """


@dataclass
class JobRoomQuery:
    """Request-Body der Such-API. Feldnamen wie vom Server erwartet."""

    since_days: int = 1
    cantons: list[str] | None = None
    keywords: list[str] | None = None
    workload_min: int = 0
    workload_max: int = 100
    permanent: bool | None = None

    def to_body(self) -> dict[str, Any]:
        return {
            "onlineSince": self.since_days,
            "cantonCodes": self.cantons or [],
            "keywords": self.keywords or [],
            "professionCodes": [],
            "communalCodes": [],
            "workloadPercentageMin": self.workload_min,
            "workloadPercentageMax": self.workload_max,
            "permanent": self.permanent,
            # Inserate, die nur für RAV-Beratende sichtbar sind, gehen uns
            # nichts an.
            "displayRestricted": False,
        }


class JobRoomFetcher:
    source = SOURCE

    def __init__(
        self,
        config: Config,
        client: PoliteClient,
        cache: RawCache,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        self.config = config
        self.client = client
        self.cache = cache
        self.conn = conn
        self._cfg = config.sources.job_room

    # -- URLs --------------------------------------------------------------

    def _search_url(self, page: int, size: int) -> str:
        return f"{self._cfg.base_url}{self._cfg.search_path}?page={page}&size={size}&sort=date_desc"

    def detail_url(self, job_id: str) -> str:
        return f"{self._cfg.base_url}{self._cfg.detail_path.format(id=job_id)}"

    # -- Hauptablauf -------------------------------------------------------

    def count(self, query: JobRoomQuery) -> int:
        """Trefferzahl über ``x-total-count``, ohne die Daten zu holen."""
        resp = self.client.post(self._search_url(0, 1), json=query.to_body())
        resp.raise_for_status()
        return int(resp.headers.get("x-total-count", 0))

    def fetch(
        self,
        *,
        since_days: int | None = None,
        query: JobRoomQuery | None = None,
    ) -> FetchResult:
        q = query or JobRoomQuery(
            since_days=since_days if since_days is not None else self._cfg.default_since_days,
            cantons=list(self.config.region.cantons),
        )
        size = self._cfg.page_size
        result = FetchResult(source=self.source)

        total = self.count(q)
        log.info(
            "job_room.count",
            total=total,
            since_days=q.since_days,
            cantons=q.cantons,
        )

        if total > MAX_RESULT_WINDOW:
            raise ResultWindowExceeded(
                f"{total} Treffer überschreiten das Server-Limit von {MAX_RESULT_WINDOW}. "
                f"Grenze die Abfrage ein — kleineres --since (z.B. {max(1, q.since_days // 2)}), "
                f"weniger Kantone, oder Keywords setzen."
            )

        if total == 0:
            log.info("job_room.empty", since_days=q.since_days)
            return result

        day = datetime.now(UTC).strftime("%Y-%m-%d")
        pages = (total + size - 1) // size

        for page in range(pages):
            resp = self.client.post(self._search_url(page, size), json=q.to_body())
            resp.raise_for_status()
            items = resp.json()
            if not items:
                break

            for wrapper in items:
                ad = wrapper.get("jobAdvertisement") or {}
                ad_id = ad.get("id")
                if not ad_id:
                    continue
                # Personendaten fliegen raus, BEVOR etwas auf Platte landet.
                # Was wir nie verwenden wollen, wird auch nicht gespeichert.
                ad = scrub_job_room(ad)
                path = self.cache.write(self.source, str(ad_id), ad, day=day)
                result.raw_paths.append(path)
                result.items_seen += 1

            log.info(
                "job_room.page",
                page=page + 1,
                of=pages,
                items=len(items),
                seen=result.items_seen,
            )

        result.requests_made = self.client.stats.requests_made
        return result

    # -- Einzelabruf -------------------------------------------------------

    def fetch_one(self, job_id: str) -> dict[str, Any]:
        """Ein einzelnes Inserat. Nützlich zum Prüfen, ob es noch online ist."""
        resp = self.client.get(self.detail_url(job_id))
        if resp.status_code == 404:
            return {}
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data
