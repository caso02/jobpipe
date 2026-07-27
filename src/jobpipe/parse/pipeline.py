"""Rohdaten einlesen, normalisieren, deduplizieren, speichern.

Arbeitet ausschliesslich auf ``data/raw/`` — nie gegen das Netz. Damit ist
dieser Schritt beliebig oft wiederholbar, ohne die Portale zu belasten.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from jobpipe.parse import ch_media as ch_media_parser
from jobpipe.parse import job_room as job_room_parser
from jobpipe.parse.agency import annotate as annotate_agencies
from jobpipe.parse.agency import summary as agency_summary
from jobpipe.parse.dedup import cluster_across_portals, collapse_company_duplicates, dedup_stats
from jobpipe.parse.geo import Geocoder
from jobpipe.parse.schema import JobPosting
from jobpipe.store import jobs as jobs_store

log = structlog.get_logger(__name__)

CH_MEDIA_PORTALS = ("myjob", "ostjob", "zentraljob")


@dataclass
class ParseReport:
    files_read: int = 0
    parsed: int = 0
    skipped: int = 0
    geocoded: int = 0
    clusters: int = 0
    cross_portal_clusters: int = 0
    representatives: int = 0
    stored_new: int = 0
    stored_updated: int = 0
    company_dedup: dict[str, int | float] = field(default_factory=dict)
    agencies: dict[str, object] = field(default_factory=dict)


def _iter_raw(raw_dir: Path, portal: str) -> Iterator[tuple[Path, dict[str, Any]]]:
    portal_dir = raw_dir / portal
    if not portal_dir.exists():
        return
    for path in sorted(portal_dir.rglob("*.json.gz")):
        try:
            with gzip.open(path, "rb") as fh:
                yield path, json.loads(fh.read().decode("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("parse.unreadable", path=str(path), error=str(exc))


def load_jobs(raw_dir: Path) -> tuple[list[JobPosting], int, int]:
    """Liest alle Rohdaten und normalisiert sie. Neueste Datei gewinnt."""
    by_key: dict[tuple[str, str], JobPosting] = {}
    files = 0
    skipped = 0

    for path, raw in _iter_raw(raw_dir, "job_room"):
        files += 1
        job = job_room_parser.parse(raw, path)
        if job:
            by_key[(job.portal, job.source_id)] = job
        else:
            skipped += 1

    for portal in CH_MEDIA_PORTALS:
        for path, raw in _iter_raw(raw_dir, portal):
            files += 1
            job = ch_media_parser.parse(raw, portal, path)
            if job:
                by_key[(job.portal, job.source_id)] = job
            else:
                skipped += 1

    return list(by_key.values()), files, skipped


def run(conn: sqlite3.Connection, raw_dir: Path) -> ParseReport:
    report = ParseReport()

    jobs, report.files_read, report.skipped = load_jobs(raw_dir)
    report.parsed = len(jobs)
    if not jobs:
        return report

    # Ort auflösen, wo die Quelle keine Koordinaten liefert (CH Media).
    geocoder = Geocoder(conn)
    for job in jobs:
        geocoder.enrich(job)
    report.geocoded = sum(1 for j in jobs if j.lat is not None)

    # Vermittler erkennen. Muss NACH dem Geocoding laufen, weil die
    # Ortsstreuung Koordinaten braucht.
    verdicts = annotate_agencies(jobs)
    report.agencies = agency_summary(jobs, verdicts)

    # Portalübergreifend gruppieren und die cluster_id auf allen Mitgliedern
    # setzen, damit der Digest später eine Stelle einmal zeigt.
    clusters = cluster_across_portals(jobs)
    for cluster in clusters:
        for member in cluster.members:
            member.cluster_id = cluster.cluster_id
    report.clusters = len(clusters)
    report.cross_portal_clusters = sum(1 for c in clusters if len(c.portals) > 1)

    # Mehrfach ausgeschriebene Stellen derselben Firma gruppieren.
    # Alle Zeilen bleiben erhalten — für den Radiusfilter zählt der einzelne
    # Ort. Markiert wird nur, was zusammengehört und welcher Eintrag im Digest
    # stellvertretend gezeigt wird.
    groups = collapse_company_duplicates(jobs)
    for group in groups:
        gid = group.representative.content_hash[:16]
        for member in [group.representative, *group.duplicates]:
            member.group_id = gid
            member.group_size = group.size
            member.is_group_representative = member is group.representative
    report.company_dedup = dedup_stats(groups)

    # Beide Dedup-Dimensionen zusammenführen.
    #
    # Sie greifen unterschiedlich: das Cluster erkennt dieselbe Stelle über
    # Portale und Rechtsträger hinweg ("Abteilungsleiter:in Auftragsabwicklung
    # 3R" lief bei Stadler Rail AG, Stadler Service AG und Stadler Rail
    # Management AG), die Firmengruppe erkennt dieselbe Stelle über viele
    # Gemeinden. Beides bedeutet fürs Ergebnis dasselbe: einmal zeigen.
    #
    # Ohne diesen Schritt tauchte die Stadler-Stelle dreimal in der Trefferliste
    # auf, weil die Firmengruppierung pro Firmenname arbeitet.
    canonical_of_cluster = {c.cluster_id: c.canonical for c in clusters}
    for job in jobs:
        if job.cluster_id and canonical_of_cluster[job.cluster_id] is not job:
            job.is_group_representative = False
    report.representatives = sum(1 for j in jobs if j.is_group_representative)

    report.stored_new, report.stored_updated = jobs_store.upsert_many(conn, jobs)
    jobs_store.mark_expired(conn)
    return report
