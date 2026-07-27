"""Täglicher Digest je Suchprofil.

Nimmt die gespeicherten Scores und macht daraus eine Liste, die man morgens
tatsächlich liest. Drei Dinge entscheiden darüber, ob das gelingt:

**Vielfalt statt Rangfolge pur.** Ein Arbeitgeber darf nur begrenzt oft
auftauchen. Ohne diese Deckelung bestand die Top-12 gemessen aus fünf
Inseraten desselben Vermittlers — technisch korrekt gerankt und trotzdem
unbrauchbar.

**Nachvollziehbarkeit.** Zu jedem Treffer steht, warum er dort steht. Ein
Ranking, dessen Zustandekommen man nicht sieht, benutzt man nach einer Woche
nicht mehr.

**Deltas.** "Neu seit dem letzten Digest" heisst: was DU noch nicht gesehen
hast — nicht, was neu im Bestand ist. Ein Inserat kann seit Tagen dort liegen
und erst heute durch veränderte Konkurrenz nach oben rutschen.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from jobpipe.config import Config, Profile

log = structlog.get_logger(__name__)

#: Inserate, die innerhalb dieser Frist ablaufen, bekommen einen eigenen
#: Abschnitt — sonst merkt man es erst, wenn die Stelle weg ist.
EXPIRING_SOON_DAYS = 7

#: Wie viele Zeichen der Beschreibung als Vorschau mitgehen.
SNIPPET_CHARS = 260


@dataclass
class DigestEntry:
    rank: int
    job_id: int
    score: float
    title: str
    company: str
    is_agency: bool
    agency_reason: str
    city: str | None
    distance_km: float | None
    workload: str
    portal: str
    url: str
    apply_url: str | None
    role: str
    reasons: dict[str, str]
    snippet: str
    posted_at: datetime | None
    expires_at: datetime | None
    group_size: int
    group_locations: list[str] = field(default_factory=list)
    is_new: bool = False
    truncated: bool = False

    @property
    def expires_in_days(self) -> int | None:
        if not self.expires_at:
            return None
        exp = self.expires_at if self.expires_at.tzinfo else self.expires_at.replace(tzinfo=UTC)
        return max(0, (exp - datetime.now(UTC)).days)


@dataclass
class Digest:
    profile_name: str
    profile_title: str
    created_at: datetime
    entries: list[DigestEntry] = field(default_factory=list)
    expiring: list[DigestEntry] = field(default_factory=list)
    total_candidates: int = 0
    new_count: int = 0
    by_role: dict[str, int] = field(default_factory=dict)
    companies_capped: int = 0
    model: str = ""

    @property
    def has_content(self) -> bool:
        return bool(self.entries)


def _snippet(text: str, limit: int = SNIPPET_CHARS) -> str:
    from jobpipe.parse.schema import strip_html, unescape_markdown

    clean = unescape_markdown(strip_html(text))
    if len(clean) <= limit:
        return clean
    cut = clean[:limit]
    # An der letzten Wortgrenze abschneiden, nicht mitten im Wort.
    space = cut.rfind(" ")
    return (cut[:space] if space > limit * 0.6 else cut).rstrip(" ,;.") + " …"


def _workload_label(lo: int | None, hi: int | None) -> str:
    if lo is None and hi is None:
        return "Pensum offen"
    if lo == hi:
        return f"{lo}%"
    return f"{lo or 0}-{hi or 100}%"


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _group_locations(conn: sqlite3.Connection, group_id: str | None, limit: int = 8) -> list[str]:
    """Orte aller Inserate derselben Gruppe.

    Bei Massenausschreibungen ("dieselbe Stelle in 48 Gemeinden") ist die
    Ortsliste die eigentliche Information — nicht der eine Ort, den der
    Vertreter zufällig trägt.
    """
    if not group_id:
        return []
    rows = conn.execute(
        "SELECT DISTINCT city FROM jobs WHERE group_id = ? AND city IS NOT NULL LIMIT ?",
        (group_id, limit),
    ).fetchall()
    return [r["city"] for r in rows]


def build(
    conn: sqlite3.Connection,
    profile: Profile,
    config: Config,
    *,
    top_n: int = 15,
    record: bool = True,
) -> Digest:
    """Baut den Digest. ``record=False`` für Vorschauen ohne Seiteneffekt."""
    now = datetime.now(UTC)
    digest = Digest(
        profile_name=profile.name,
        profile_title=profile.display_name,
        created_at=now,
    )

    rows = conn.execute(
        """SELECT s.job_rowid, s.final_score, s.best_role, s.rules_json, s.distance_km,
                  j.title, j.company_name, j.company_is_agency, j.agency_reason,
                  j.city, j.workload_min, j.workload_max, j.portal, j.source_url,
                  j.apply_url, j.description_md, j.description_truncated,
                  j.posted_at, j.expires_at, j.group_id, j.group_size,
                  (ds.job_rowid IS NULL) AS is_new
             FROM scores s
             JOIN jobs j ON j.id = s.job_rowid
        LEFT JOIN digest_seen ds
               ON ds.job_rowid = s.job_rowid AND ds.profile = s.profile
            WHERE s.profile = ? AND j.status = 'active'
         ORDER BY s.final_score DESC""",
        (profile.name,),
    ).fetchall()

    digest.total_candidates = len(rows)
    if not rows:
        return digest

    cap = config.quality.max_per_company_in_digest
    per_company: Counter[str] = Counter()
    capped = 0

    for row in rows:
        if len(digest.entries) >= top_n:
            break
        company = row["company_name"]
        if per_company[company] >= cap:
            capped += 1
            continue
        per_company[company] += 1

        rules = json.loads(row["rules_json"] or "{}")
        entry = DigestEntry(
            rank=len(digest.entries) + 1,
            job_id=int(row["job_rowid"]),
            score=float(row["final_score"]),
            title=row["title"],
            company=company,
            is_agency=bool(row["company_is_agency"]),
            agency_reason=row["agency_reason"] or "",
            city=row["city"],
            distance_km=row["distance_km"],
            workload=_workload_label(row["workload_min"], row["workload_max"]),
            portal=row["portal"],
            url=row["source_url"],
            apply_url=row["apply_url"],
            role=row["best_role"] or "",
            reasons={k: v for k, v in (rules.get("reasons") or {}).items() if v},
            snippet=_snippet(row["description_md"] or ""),
            posted_at=_parse_dt(row["posted_at"]),
            expires_at=_parse_dt(row["expires_at"]),
            group_size=int(row["group_size"] or 1),
            group_locations=_group_locations(conn, row["group_id"])
            if (row["group_size"] or 1) > 1
            else [],
            is_new=bool(row["is_new"]),
            truncated=bool(row["description_truncated"]),
        )
        digest.entries.append(entry)

    digest.companies_capped = capped
    digest.new_count = sum(1 for e in digest.entries if e.is_new)
    digest.by_role = dict(Counter(e.role for e in digest.entries if e.role))

    # Bald ablaufende Treffer aus dem gesamten bewerteten Bestand, nicht nur
    # aus den Top-N: eine gute Stelle auf Rang 30, die morgen zugeht, ist
    # dringender als eine auf Rang 5, die noch drei Wochen läuft.
    horizon = now + timedelta(days=EXPIRING_SOON_DAYS)
    shown_ids = {e.job_id for e in digest.entries}
    for row in rows[: top_n * 4]:
        exp = _parse_dt(row["expires_at"])
        if not exp:
            continue
        exp = exp if exp.tzinfo else exp.replace(tzinfo=UTC)
        if now <= exp <= horizon and int(row["job_rowid"]) not in shown_ids:
            digest.expiring.append(
                DigestEntry(
                    rank=0,
                    job_id=int(row["job_rowid"]),
                    score=float(row["final_score"]),
                    title=row["title"],
                    company=row["company_name"],
                    is_agency=bool(row["company_is_agency"]),
                    agency_reason=row["agency_reason"] or "",
                    city=row["city"],
                    distance_km=row["distance_km"],
                    workload=_workload_label(row["workload_min"], row["workload_max"]),
                    portal=row["portal"],
                    url=row["source_url"],
                    apply_url=row["apply_url"],
                    role=row["best_role"] or "",
                    reasons={},
                    snippet="",
                    posted_at=_parse_dt(row["posted_at"]),
                    expires_at=exp,
                    group_size=int(row["group_size"] or 1),
                )
            )
        if len(digest.expiring) >= 5:
            break

    if record:
        _record(conn, profile.name, digest)

    return digest


def _record(conn: sqlite3.Connection, profile_name: str, digest: Digest) -> None:
    """Merkt sich, was gezeigt wurde — Grundlage der Neu-Erkennung."""
    stamp = digest.created_at.isoformat(timespec="seconds")
    conn.executemany(
        "INSERT OR IGNORE INTO digest_seen (profile, job_rowid, first_shown_at) VALUES (?,?,?)",
        [(profile_name, e.job_id, stamp) for e in digest.entries],
    )
    conn.execute(
        "INSERT INTO digest_runs (profile, created_at, shown, new_count) VALUES (?,?,?,?)",
        (profile_name, stamp, len(digest.entries), digest.new_count),
    )


def last_run(conn: sqlite3.Connection, profile_name: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM digest_runs WHERE profile = ? ORDER BY created_at DESC LIMIT 1",
        (profile_name,),
    ).fetchone()
    return row


def stats(conn: sqlite3.Connection, profile_name: str) -> dict[str, Any]:
    runs = conn.execute(
        "SELECT COUNT(*) n FROM digest_runs WHERE profile = ?", (profile_name,)
    ).fetchone()["n"]
    seen = conn.execute(
        "SELECT COUNT(*) n FROM digest_seen WHERE profile = ?", (profile_name,)
    ).fetchone()["n"]
    return {"digests": runs, "bereits_gezeigt": seen}
