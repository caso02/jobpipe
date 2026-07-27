"""Semantik und Regeln zu einem Ranking verbinden — pro Suchprofil.

Ablauf:

1. Kandidaten laden (nur anzuzeigende Stellen, siehe Dedup in M2)
2. Inserate einbetten (gecacht, profil-unabhängig)
3. Zielrollen einbetten, beste Rolle je Inserat bestimmen
4. Cosine-Werte im Pool rangnormalisieren
5. Regeln anwenden, gewichtet aufsummieren, Strafen abziehen
6. Alles inklusive Teilscores speichern
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

from jobpipe.config import Profile
from jobpipe.enrich import rules as rules_mod
from jobpipe.enrich.embed import (
    EmbeddingCache,
    build_embedder,
    build_job_text,
    build_role_text,
    cosine_max,
    spread_scores,
)
from jobpipe.parse.schema import JobPosting

log = structlog.get_logger(__name__)


@dataclass
class ScoreReport:
    profile: str
    model: str = ""
    candidates: int = 0
    scored: int = 0
    embeddings_cached: int = 0
    embeddings_computed: int = 0
    top: list[dict[str, Any]] = field(default_factory=list)


def _load_candidates(conn: sqlite3.Connection) -> list[tuple[int, JobPosting]]:
    """Aktive Stellen, die nach der Deduplizierung angezeigt werden sollen."""
    rows = conn.execute(
        """SELECT id, portal, source_id, source_url, title, company_name,
                  company_is_agency, description_md, description_truncated,
                  city, postal_code, canton, lat, lon,
                  workload_min, workload_max, home_office,
                  posted_at, expires_at, status, apply_url,
                  group_size, categories
             FROM jobs
            WHERE status = 'active' AND is_group_representative = 1"""
    ).fetchall()

    out: list[tuple[int, JobPosting]] = []
    for r in rows:
        job = JobPosting(
            portal=r["portal"],
            source_id=r["source_id"],
            source_url=r["source_url"],
            title=r["title"],
            company_name=r["company_name"],
            company_is_agency=bool(r["company_is_agency"]),
            description_md=r["description_md"] or "",
            description_truncated=bool(r["description_truncated"]),
            city=r["city"],
            postal_code=r["postal_code"],
            canton=r["canton"],
            lat=r["lat"],
            lon=r["lon"],
            workload_min=r["workload_min"],
            workload_max=r["workload_max"],
            home_office=None if r["home_office"] is None else bool(r["home_office"]),
            posted_at=datetime.fromisoformat(r["posted_at"]) if r["posted_at"] else None,
            expires_at=datetime.fromisoformat(r["expires_at"]) if r["expires_at"] else None,
            status=r["status"],
            apply_url=r["apply_url"],
            group_size=r["group_size"],
            categories=json.loads(r["categories"] or "[]"),
        )
        out.append((int(r["id"]), job))
    return out


def run(
    conn: sqlite3.Connection,
    profile: Profile,
    *,
    prefer_local: bool = True,
    use_gemini: bool = False,
    top_n: int = 20,
) -> ScoreReport:
    report = ScoreReport(profile=profile.name)
    candidates = _load_candidates(conn)
    report.candidates = len(candidates)
    if not candidates:
        return report

    embedder = build_embedder(prefer_local=prefer_local, use_gemini=use_gemini)
    report.model = embedder.name
    cache = EmbeddingCache(conn, embedder)

    # 1) Inserate einbetten. Profil-unabhängig, also über Profile hinweg gecacht.
    job_texts = [
        build_job_text(job.title, job.company_name, job.description_md) for _, job in candidates
    ]
    job_vecs = cache.embed(job_texts)

    # 2) Zielrollen einbetten. Wenige Texte, kaum Kosten.
    role_texts = [build_role_text(r.name, r.description) for r in profile.target_roles]
    role_vecs = cache.embed(role_texts) if role_texts else []

    report.embeddings_cached = cache.stats.from_cache
    report.embeddings_computed = cache.stats.computed

    # 3) Beste Zielrolle je Inserat.
    raw_sims: list[float] = []
    best_roles: list[str] = []
    for vec in job_vecs:
        sim, idx = cosine_max(vec, role_vecs)
        raw_sims.append(sim)
        best_roles.append(profile.target_roles[idx].name if idx >= 0 else "")

    # 4) Enges Cosine-Band auf 0..1 dehnen. Ohne diesen Schritt wäre der
    #    semantische Anteil praktisch konstant und damit wirkungslos.
    spread = spread_scores(raw_sims)

    # 5) Regeln und Gesamtscore.
    w = profile.weights
    now = datetime.now(UTC).isoformat(timespec="seconds")
    results: list[tuple[int, JobPosting, float, float, float, str, rules_mod.RuleResult]] = []

    for (job_id, job), sim, sp, role in zip(candidates, raw_sims, spread, best_roles, strict=True):
        r = rules_mod.evaluate(job, profile)
        s = r.scores
        positive = (
            w.semantic * sp
            + w.workload * s["workload"]
            + w.location * s["location"]
            + w.home_office * s["home_office"]
            + w.flextime * s["flextime"]
            + w.keywords * s["keywords"]
            + w.recency * s["recency"]
        )
        penalty = (
            w.exclude_penalty * s["exclude_penalty"]
            + w.agency_penalty * s["agency_penalty"]
            + w.seniority_penalty * s["seniority_penalty"]
        )
        total_weight = (
            w.semantic
            + w.workload
            + w.location
            + w.home_office
            + w.flextime
            + w.keywords
            + w.recency
        )
        final = (positive - penalty) / total_weight
        results.append((job_id, job, final, sim, sp, role, r))

    # 6) Speichern.
    conn.execute("DELETE FROM scores WHERE profile = ?", (profile.name,))
    conn.executemany(
        """INSERT INTO scores (profile, job_rowid, final_score, semantic_raw,
                               semantic_spread, best_role, rules_json,
                               distance_km, computed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                profile.name,
                job_id,
                final,
                sim,
                sp,
                role,
                json.dumps(r.as_json(), ensure_ascii=False),
                r.distance_km,
                now,
            )
            for job_id, _job, final, sim, sp, role, r in results
        ],
    )
    report.scored = len(results)

    results.sort(key=lambda t: t[2], reverse=True)
    report.top = [
        {
            "score": round(final, 3),
            "title": job.title,
            "company": job.company_name,
            "city": job.city,
            "portal": job.portal,
            "url": job.source_url,
            "role": role,
            "semantic_raw": round(sim, 3),
            "distance_km": r.distance_km,
            "group_size": job.group_size,
            "is_agency": job.company_is_agency,
            "reasons": r.reasons,
        }
        for _job_id, job, final, sim, _sp, role, r in results[:top_n]
    ]
    return report
