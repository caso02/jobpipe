"""Skill-Extraktion und Gap-Analyse.

Beantwortet: **was verlangen die Stellen, die mich interessieren, und was davon
steht nicht in meinem CV?** Das ist der Teil, der über die nächste Bewerbung
hinaus nützt — er sagt, worin sich Lernen lohnt.

Wörterbuch statt LLM
--------------------
Die Extraktion läuft über ein kuratiertes Wörterbuch mit Aliassen, nicht über
ein Sprachmodell. Drei Gründe: sie ist erklärbar (man sieht, warum ein Skill
erkannt wurde), sie kostet nichts, und sie ist stabil — dieselbe Eingabe
liefert morgen dasselbe Ergebnis.

Zwei Quellen speisen das Wörterbuch:

* eine gepflegte **Tech- und Fachbegriffsliste** (``data/ref/skills.yaml``)
* das **Tätigkeitsvokabular** der 1'851 Schweizer Berufe aus dem
  DSP-Vorprojekt — deutschsprachig und in genau der Sprache, in der Schweizer
  Inserate geschrieben sind
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
import yaml

log = structlog.get_logger(__name__)

DEFAULT_SKILLS_PATH = Path("data/ref/skills.yaml")

#: Ab dieser Häufigkeit im Zielcluster gilt ein Skill als gefordert.
RELEVANT_SHARE = 0.15


@dataclass
class Skill:
    name: str
    aliases: list[str] = field(default_factory=list)
    category: str = "sonstiges"

    def pattern(self) -> re.Pattern[str]:
        """Erkennungsmuster mit Wortgrenzen.

        Zwei Fallen, beide durch Tests aufgedeckt:

        * ``(?!\\w)`` verhindert, dass "Go" in "Google" trifft.
        * Ein **folgender Punkt** darf nicht generell verboten sein. Die erste
          Fassung nutzte ``(?![\\w.])``, um "Node.js" zusammenzuhalten — damit
          fiel aber jeder Skill am Satzende durch: "Ich kann Python und SQL."
          fand kein SQL. Verboten ist deshalb nur ein Punkt, auf den ein
          Wortzeichen folgt.
        """
        forms = [self.name, *self.aliases]
        alts = "|".join(re.escape(f) for f in sorted(forms, key=len, reverse=True))
        return re.compile(rf"(?<!\w)(?<!\w\.)({alts})(?!\w)(?!\.\w)", re.IGNORECASE)


@dataclass
class GapEntry:
    skill: str
    category: str
    demand: int
    share: float
    in_cv: bool

    @property
    def is_gap(self) -> bool:
        return not self.in_cv and self.share >= RELEVANT_SHARE


@dataclass
class GapReport:
    cluster_name: str = ""
    jobs_analysed: int = 0
    have: list[GapEntry] = field(default_factory=list)
    gaps: list[GapEntry] = field(default_factory=list)
    career_hints: list[str] = field(default_factory=list)


def load_skills(path: Path | None = None) -> list[Skill]:
    """Lädt das Wörterbuch. Fehlt die Datei, bleibt die Liste leer."""
    p = path or DEFAULT_SKILLS_PATH
    if not p.exists():
        log.warning("skills.missing", path=str(p))
        return []
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    out: list[Skill] = []
    for category, entries in raw.items():
        for entry in entries or []:
            if isinstance(entry, str):
                out.append(Skill(name=entry, category=category))
            elif isinstance(entry, dict):
                for name, aliases in entry.items():
                    out.append(Skill(name=name, aliases=list(aliases or []), category=category))
    return out


def extract(text: str, skills: list[Skill]) -> set[str]:
    """Welche Skills kommen im Text vor?"""
    found: set[str] = set()
    for skill in skills:
        if skill.pattern().search(text):
            found.add(skill.name)
    return found


def vocabulary_from_berufe(conn: sqlite3.Connection, berufsfeld: str | None = None) -> list[str]:
    """Tätigkeitsbegriffe aus der Schweizer Berufe-Taxonomie.

    Liefert die Kategorienamen (beim Data Scientist etwa "Datenbewirtschaftung",
    "Datenanalyse", "Algorithmen") — kurze, aussagekräftige Begriffe in der
    Sprache, in der hiesige Inserate formuliert sind.
    """
    query = "SELECT taetigkeiten FROM berufe"
    params: tuple[Any, ...] = ()
    if berufsfeld:
        query += " WHERE berufsfeld = ?"
        params = (berufsfeld,)

    terms: Counter[str] = Counter()
    for row in conn.execute(query, params):
        try:
            kategorien = json.loads(row["taetigkeiten"] or "{}")
        except json.JSONDecodeError:
            continue
        for name in kategorien:
            clean = name.strip()
            if 4 <= len(clean) <= 45:
                terms[clean] += 1
    return [t for t, _ in terms.most_common(60)]


def analyse_gap(
    conn: sqlite3.Connection,
    cluster_label: int,
    cv_text: str,
    skills: list[Skill],
    min_jobs: int = 10,
) -> GapReport:
    """Vergleicht die Anforderungen eines Clusters mit dem CV."""
    row = conn.execute("SELECT name FROM clusters WHERE label = ?", (cluster_label,)).fetchone()
    report = GapReport(cluster_name=row["name"] if row else f"Cluster {cluster_label}")

    jobs = conn.execute(
        """SELECT j.title, j.description_md FROM job_clusters jc
             JOIN jobs j ON j.id = jc.job_rowid
            WHERE jc.cluster_label = ? AND j.status = 'active'""",
        (cluster_label,),
    ).fetchall()
    report.jobs_analysed = len(jobs)
    if len(jobs) < min_jobs or not skills:
        return report

    demand: Counter[str] = Counter()
    for job in jobs:
        text = f"{job['title']} {job['description_md'] or ''}"
        demand.update(extract(text, skills))

    cv_skills = extract(cv_text, skills)
    by_name = {s.name: s for s in skills}

    for name, count in demand.most_common():
        entry = GapEntry(
            skill=name,
            category=by_name[name].category if name in by_name else "sonstiges",
            demand=count,
            share=round(count / len(jobs), 3),
            in_cv=name in cv_skills,
        )
        if entry.in_cv:
            report.have.append(entry)
        elif entry.is_gap:
            report.gaps.append(entry)

    report.career_hints = _career_hints(conn, report.cluster_name)
    return report


def _career_hints(conn: sqlite3.Connection, cluster_name: str) -> list[str]:
    """Typische Weiterbildungsschritte aus der Berufe-Taxonomie.

    Macht aus "dir fehlt X" ein "dafür gibt es diesen Weg".
    """
    beruf = cluster_name.split("→")[-1].strip()
    row = conn.execute("SELECT career_progression FROM berufe WHERE title = ?", (beruf,)).fetchone()
    if not row:
        return []
    try:
        steps = json.loads(row["career_progression"] or "[]")
    except json.JSONDecodeError:
        return []
    hints: list[str] = []
    for step in steps[:3]:
        if isinstance(step, dict):
            typ = step.get("type") or ""
            options = step.get("options") or []
            first = options[0] if options and isinstance(options[0], str) else ""
            if typ:
                hints.append(f"{typ}{': ' + first[:70] if first else ''}")
    return hints
