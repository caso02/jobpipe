"""Inserate speichern und abfragen."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import structlog

from jobpipe.parse.schema import JobPosting

log = structlog.get_logger(__name__)


def _row(job: JobPosting, now: str) -> dict[str, Any]:
    return {
        "portal": job.portal,
        "source_id": job.source_id,
        "source_url": job.source_url,
        "cluster_id": job.cluster_id,
        "title": job.title,
        "company_name": job.company_name,
        "company_is_agency": int(job.company_is_agency),
        "agency_reason": job.agency_reason,
        "description_md": job.description_md,
        "description_truncated": int(job.description_truncated),
        "description_language": job.description_language,
        "city": job.city,
        "postal_code": job.postal_code,
        "canton": job.canton,
        "lat": job.lat,
        "lon": job.lon,
        "workload_min": job.workload_min,
        "workload_max": job.workload_max,
        "is_permanent": None if job.is_permanent is None else int(job.is_permanent),
        "start_date": job.start_date.isoformat() if job.start_date else None,
        "home_office": None if job.home_office is None else int(job.home_office),
        "salary_min": job.salary_min,
        "salary_max": job.salary_max,
        "posted_at": job.posted_at.isoformat() if job.posted_at else None,
        "expires_at": job.expires_at.isoformat() if job.expires_at else None,
        "status": job.status,
        "apply_url": job.apply_url,
        "occupation_codes": json.dumps(job.occupation_codes, ensure_ascii=False),
        "categories": json.dumps(job.categories, ensure_ascii=False),
        "languages": json.dumps(job.languages, ensure_ascii=False),
        "content_hash": job.content_hash,
        "group_id": job.group_id,
        "group_size": job.group_size,
        "is_group_representative": int(job.is_group_representative),
        "now": now,
        "raw_path": job.raw_path,
    }


_UPSERT = """
INSERT INTO jobs (
    portal, source_id, source_url, cluster_id, title, company_name,
    company_is_agency, agency_reason, description_md, description_truncated,
    description_language,
    city, postal_code, canton, lat, lon,
    workload_min, workload_max, is_permanent, start_date, home_office,
    salary_min, salary_max, posted_at, expires_at, status,
    apply_url, occupation_codes, categories, languages,
    content_hash, group_id, group_size, is_group_representative,
    first_seen_at, last_seen_at, raw_path
) VALUES (
    :portal, :source_id, :source_url, :cluster_id, :title, :company_name,
    :company_is_agency, :agency_reason, :description_md, :description_truncated,
    :description_language,
    :city, :postal_code, :canton, :lat, :lon,
    :workload_min, :workload_max, :is_permanent, :start_date, :home_office,
    :salary_min, :salary_max, :posted_at, :expires_at, :status,
    :apply_url, :occupation_codes, :categories, :languages,
    :content_hash, :group_id, :group_size, :is_group_representative,
    :now, :now, :raw_path
)
ON CONFLICT (portal, source_id) DO UPDATE SET
    cluster_id = excluded.cluster_id,
    title = excluded.title,
    company_name = excluded.company_name,
    company_is_agency = excluded.company_is_agency,
    agency_reason = excluded.agency_reason,
    description_md = excluded.description_md,
    description_truncated = excluded.description_truncated,
    description_language = excluded.description_language,
    city = excluded.city,
    postal_code = excluded.postal_code,
    canton = excluded.canton,
    lat = excluded.lat,
    lon = excluded.lon,
    workload_min = excluded.workload_min,
    workload_max = excluded.workload_max,
    home_office = excluded.home_office,
    expires_at = excluded.expires_at,
    status = excluded.status,
    apply_url = excluded.apply_url,
    content_hash = excluded.content_hash,
    group_id = excluded.group_id,
    group_size = excluded.group_size,
    is_group_representative = excluded.is_group_representative,
    last_seen_at = excluded.last_seen_at
"""


def upsert_many(conn: sqlite3.Connection, jobs: Sequence[JobPosting]) -> tuple[int, int]:
    """Speichert Inserate. Gibt (neu, aktualisiert) zurück.

    ``first_seen_at`` bleibt bei bestehenden Zeilen erhalten — sonst ginge
    verloren, wann eine Stelle zum ersten Mal auftauchte.
    """
    if not jobs:
        return 0, 0
    now = datetime.now(UTC).isoformat(timespec="seconds")
    before = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    conn.executemany(_UPSERT, [_row(j, now) for j in jobs])
    after = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    new = after - before
    return new, len(jobs) - new


def mark_expired(conn: sqlite3.Connection) -> int:
    """Setzt Inserate mit abgelaufenem ``expires_at`` auf ``expired``.

    Gelöscht wird nichts — die Historie ist für die Marktanalyse wertvoll.
    """
    now = datetime.now(UTC).isoformat(timespec="seconds")
    cur = conn.execute(
        "UPDATE jobs SET status = 'expired' WHERE status = 'active' AND expires_at IS NOT NULL"
        " AND expires_at < ?",
        (now,),
    )
    return cur.rowcount or 0


def counts(conn: sqlite3.Connection) -> dict[str, Any]:
    total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    per_portal = {
        r["portal"]: r["c"]
        for r in conn.execute("SELECT portal, COUNT(*) c FROM jobs GROUP BY portal ORDER BY c DESC")
    }
    active = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='active'").fetchone()[0]
    agencies = conn.execute("SELECT COUNT(*) FROM jobs WHERE company_is_agency=1").fetchone()[0]
    clusters = conn.execute(
        "SELECT COUNT(DISTINCT cluster_id) FROM jobs WHERE cluster_id IS NOT NULL"
    ).fetchone()[0]
    geo = conn.execute("SELECT COUNT(*) FROM jobs WHERE lat IS NOT NULL").fetchone()[0]
    reps = conn.execute("SELECT COUNT(*) FROM jobs WHERE is_group_representative = 1").fetchone()[0]
    rep_agencies = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE is_group_representative = 1 AND company_is_agency = 1"
    ).fetchone()[0]
    return {
        "total": total,
        "active": active,
        "per_portal": per_portal,
        "agencies": agencies,
        "agency_share": round(100 * agencies / total, 1) if total else 0.0,
        "clusters": clusters,
        "geocoded": geo,
        "geocoded_share": round(100 * geo / total, 1) if total else 0.0,
        "representatives": reps,
        "rep_agency_share": round(100 * rep_agencies / reps, 1) if reps else 0.0,
    }


def top_companies(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    rows: list[sqlite3.Row] = conn.execute(
        """SELECT company_name, COUNT(*) c,
                  COUNT(DISTINCT content_hash) distinct_texts
             FROM jobs
         GROUP BY company_name
         ORDER BY c DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return rows
