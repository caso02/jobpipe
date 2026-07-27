"""Fetcher für die CH-Media-Portale: myjob.ch, ostjob.ch, zentraljob.ch.

Ein Adapter, mehrere Hosts. Verifiziert identisch bei allen dreien: dieselbe
robots.txt (inklusive derselben namentlich gesperrten Job-Bots), dieselbe
``sitemap-vacancies*.xml``-Struktur, dasselbe ``window.__PRELOADED_STATE__``
mit ``vacancyDetails.data``. Betreiberin ist jeweils CH Media Classifieds AG.

Warum nicht einfach alles holen
-------------------------------
Die Sitemaps führen 53'031 Inserate (myjob) bzw. 6'119 (ostjob). Bei den
konfigurierten 1 req/s wäre ein Vollcrawl ein 15-Stunden-Dauerfeuer auf fremde
Server — unverhältnismässig für eine private Stellensuche.

``lastmod`` hilft nicht: bei myjob tragen 43'952 Einträge dasselbe Datum, das
stammt aus einer Massen-Regenerierung und nicht aus echten Änderungen.

Die IDs sind dagegen aufsteigend vergeben und die Sitemap absteigend sortiert.
Daraus folgt das Vorgehen:

1. **Seed** (Standard beim ersten Lauf): Sitemap lesen, alle IDs in
   ``source_items`` vermerken, **keine** Detailseite holen. Kostet 1-2 Requests.
2. **Inkrementell**: bei jedem weiteren Lauf nur Detailseiten zu IDs holen, die
   vorher nicht bekannt waren.
3. **Backfill mit Budget**: ``--backfill N`` arbeitet den Altbestand in Ruhe ab,
   N Inserate pro Lauf. Verteilt die Last über Tage statt sie zu bündeln.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from selectolax.parser import HTMLParser

from jobpipe.collect.base import FetchResult, PoliteClient, RawCache
from jobpipe.config import ChMediaHostConfig, Config
from jobpipe.parse.pii import scrub_ch_media

log = structlog.get_logger(__name__)

_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")
_URL_BLOCK_RE = re.compile(r"<url>(.*?)</url>", re.S)
_LASTMOD_RE = re.compile(r"<lastmod>\s*([^<\s]+)\s*</lastmod>")
_STATE_MARKER = "window.__PRELOADED_STATE__"

#: /job/<slug>/<id> — nur die Zahl am Ende ist stabil, der Slug ist Dekoration.
_JOB_ID_RE = re.compile(r"/job/[^/]+/(\d+)")


class PreloadedStateMissing(RuntimeError):
    """Die Detailseite enthält kein ``__PRELOADED_STATE__``.

    Tritt auf, wenn das Portal sein Frontend umbaut. Bewusst laut, damit der
    Parser nicht still leere Datensätze produziert.
    """


@dataclass
class SitemapEntry:
    source_id: str
    url: str
    lastmod: str | None


def parse_sitemap(xml: str) -> list[SitemapEntry]:
    """Liest ``<url>``-Blöcke. Reihenfolge bleibt erhalten (neueste zuerst)."""
    entries: list[SitemapEntry] = []
    for block in _URL_BLOCK_RE.findall(xml):
        loc_match = _LOC_RE.search(block)
        if not loc_match:
            continue
        url = loc_match.group(1)
        id_match = _JOB_ID_RE.search(url)
        if not id_match:
            continue
        lastmod_match = _LASTMOD_RE.search(block)
        entries.append(
            SitemapEntry(
                source_id=id_match.group(1),
                url=url,
                lastmod=lastmod_match.group(1) if lastmod_match else None,
            )
        )
    return entries


def extract_preloaded_state(html: str) -> dict[str, Any]:
    """Holt das JSON-Objekt hinter ``window.__PRELOADED_STATE__ =``.

    Klammerzählung statt Regex, weil der Blob verschachtelt ist und Strings
    mit geschweiften Klammern enthält.
    """
    start = html.find(_STATE_MARKER)
    if start < 0:
        raise PreloadedStateMissing("window.__PRELOADED_STATE__ nicht gefunden")
    eq = html.find("=", start + len(_STATE_MARKER))
    if eq < 0:
        raise PreloadedStateMissing("Zuweisung nach __PRELOADED_STATE__ fehlt")

    tail = html[eq + 1 :]
    depth = 0
    in_string = False
    escaped = False
    begin = -1
    for i, ch in enumerate(tail):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                begin = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and begin >= 0:
                try:
                    parsed: dict[str, Any] = json.loads(tail[begin : i + 1])
                except json.JSONDecodeError as exc:
                    raise PreloadedStateMissing(f"State ist kein gültiges JSON: {exc}") from exc
                return parsed
    raise PreloadedStateMissing("Klammern im State-Objekt schliessen nicht")


def extract_vacancy(html: str) -> dict[str, Any]:
    """Liefert ``vacancyDetails.data`` aus einer Detailseite."""
    state = extract_preloaded_state(html)
    details = state.get("vacancyDetails")
    if not isinstance(details, dict):
        raise PreloadedStateMissing("vacancyDetails fehlt im State")
    data = details.get("data")
    if not isinstance(data, dict) or not data:
        raise PreloadedStateMissing("vacancyDetails.data ist leer")
    return data


def looks_like_expired(html: str) -> bool:
    """Erkennt die Seite eines abgelaufenen Inserats.

    Zusätzlich zum ``410 Gone``, das ostjob und myjob sauber liefern.
    """
    tree = HTMLParser(html)
    title = tree.css_first("title")
    text = (title.text() if title else "").lower()
    return "nicht mehr" in text or "abgelaufen" in text or "gone" in text


class ChMediaFetcher:
    """Holt Inserate eines CH-Media-Portals."""

    def __init__(
        self,
        portal: str,
        host: ChMediaHostConfig,
        config: Config,
        client: PoliteClient,
        cache: RawCache,
        conn: sqlite3.Connection,
    ) -> None:
        self.source = portal
        self.host = host
        self.config = config
        self.client = client
        self.cache = cache
        self.conn = conn

    # -- Sitemap -----------------------------------------------------------

    def read_sitemaps(self) -> list[SitemapEntry]:
        entries: list[SitemapEntry] = []
        for path in self.host.sitemaps:
            resp = self.client.get(f"{self.host.base_url}{path}")
            resp.raise_for_status()
            found = parse_sitemap(resp.text)
            log.info("ch_media.sitemap", portal=self.source, path=path, entries=len(found))
            entries.extend(found)
        return entries

    def record_entries(self, entries: list[SitemapEntry]) -> int:
        """Vermerkt IDs als bekannt. Gibt die Zahl der NEUEN zurück."""
        now = datetime.now(UTC).isoformat(timespec="seconds")
        before = self.conn.execute(
            "SELECT COUNT(*) FROM source_items WHERE portal = ?", (self.source,)
        ).fetchone()[0]
        self.conn.executemany(
            """INSERT OR IGNORE INTO source_items
                 (portal, source_id, url, lastmod, first_seen_at)
               VALUES (?, ?, ?, ?, ?)""",
            [(self.source, e.source_id, e.url, e.lastmod, now) for e in entries],
        )
        after = self.conn.execute(
            "SELECT COUNT(*) FROM source_items WHERE portal = ?", (self.source,)
        ).fetchone()[0]
        return int(after - before)

    def pending(self, limit: int) -> list[sqlite3.Row]:
        """Bekannte IDs, deren Detailseite noch fehlt. Neueste zuerst."""
        rows: list[sqlite3.Row] = self.conn.execute(
            """SELECT source_id, url FROM source_items
                WHERE portal = ? AND fetched_at IS NULL
             ORDER BY CAST(source_id AS INTEGER) DESC
                LIMIT ?""",
            (self.source, limit),
        ).fetchall()
        return rows

    # -- Detailseiten ------------------------------------------------------

    def fetch_detail(self, source_id: str, url: str, day: str) -> tuple[int, str | None]:
        """Holt eine Detailseite. Gibt (HTTP-Status, Rohdatenpfad) zurück."""
        resp = self.client.get(url)

        if resp.status_code in (404, 410):
            # ostjob/myjob antworten sauber mit 410 Gone, wenn die Stelle weg ist.
            self._mark(source_id, resp.status_code, None)
            return resp.status_code, None

        resp.raise_for_status()
        if looks_like_expired(resp.text):
            self._mark(source_id, 410, None)
            return 410, None

        data = extract_vacancy(resp.text)
        # Personendaten raus, bevor irgendetwas auf Platte landet.
        data = scrub_ch_media(data)
        data["_source_url"] = url
        path = self.cache.write(self.source, source_id, data, day=day)
        self._mark(source_id, resp.status_code, str(path))
        return resp.status_code, str(path)

    def _mark(self, source_id: str, status: int, raw_path: str | None) -> None:
        self.conn.execute(
            """UPDATE source_items
                  SET fetched_at = ?, http_status = ?, raw_path = ?
                WHERE portal = ? AND source_id = ?""",
            (
                datetime.now(UTC).isoformat(timespec="seconds"),
                status,
                raw_path,
                self.source,
                source_id,
            ),
        )

    # -- Hauptablauf -------------------------------------------------------

    def fetch(
        self,
        *,
        since_days: int | None = None,
        seed_only: bool = False,
        max_details: int = 0,
    ) -> FetchResult:
        """Sitemap lesen, dann Detailseiten bis zum Budget.

        ``seed_only`` merkt sich nur die IDs (1-2 Requests, keine Detailseiten).
        ``max_details`` begrenzt die Detailabrufe pro Lauf; 0 bedeutet, dass nur
        die in diesem Lauf neu entdeckten Inserate geholt werden.
        """
        result = FetchResult(source=self.source)
        entries = self.read_sitemaps()
        new_count = self.record_entries(entries)
        result.items_seen = len(entries)
        result.items_new = new_count

        log.info(
            "ch_media.seeded",
            portal=self.source,
            in_sitemap=len(entries),
            new_ids=new_count,
        )

        if seed_only:
            result.requests_made = self.client.stats.requests_made
            return result

        budget = max_details if max_details > 0 else new_count
        if budget <= 0:
            result.requests_made = self.client.stats.requests_made
            return result

        day = datetime.now(UTC).strftime("%Y-%m-%d")
        gone = 0
        failed = 0
        for row in self.pending(budget):
            try:
                status, path = self.fetch_detail(row["source_id"], row["url"], day)
            except PreloadedStateMissing as exc:
                # Ein einzelnes kaputtes Inserat darf den Lauf nicht kippen,
                # aber es soll sichtbar sein.
                log.warning(
                    "ch_media.state_missing",
                    portal=self.source,
                    source_id=row["source_id"],
                    error=str(exc),
                )
                self._mark(row["source_id"], 0, None)
                failed += 1
                continue
            if status in (404, 410):
                gone += 1
            elif path:
                result.raw_paths.append(self.cache.path_for(self.source, row["source_id"], day))
                result.items_updated += 1

        log.info(
            "ch_media.details",
            portal=self.source,
            fetched=result.items_updated,
            gone=gone,
            failed=failed,
        )
        result.requests_made = self.client.stats.requests_made
        return result
