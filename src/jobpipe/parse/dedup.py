"""Deduplizierung in drei Stufen.

1. **Exakt** — ``(portal, source_id)`` ist der Primärschlüssel, erledigt die
   Datenbank.
2. **Portalübergreifend** — dieselbe Stelle auf job-room *und* myjob/ostjob.
   Blocking auf PLZ, dann gewichteter Fuzzy-Score auf Titel, Firma und Ort.
   Gleiche Bewerbungs-URL ist ein so starkes Signal, dass es die Fuzzy-Schwelle
   überstimmt.
3. **Near-Duplicates innerhalb einer Firma** — der eigentliche Grund für dieses
   Modul. Gemessen stammen 50 % der job-room-Inserate von Personalvermittlern,
   ein einziger Anbieter stellt 39.5 % und schreibt dieselbe Stelle mit
   ortsvariierten Textbausteinen dutzendfach aus. Ohne diesen Schritt besteht
   das Ranking zur Hälfte aus Rauschen.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlparse

import structlog
from rapidfuzz import fuzz

from jobpipe.parse.schema import JobPosting, strip_html

log = structlog.get_logger(__name__)

#: Ab hier gelten zwei Inserate als dieselbe Stelle.
CROSS_PORTAL_THRESHOLD = 0.87

#: Der Titel muss diese Hürde unabhängig nehmen.
#:
#: Ohne sie entscheidet bei gleicher Firma am gleichen Ort faktisch nichts mehr:
#: Firma (0.35) und Ort (0.20) bringen 0.55 mit, der Titel müsste nur noch 0.71
#: erreichen. Gemessen an echten Daten führte das dazu, dass "Testingenieur:in
#: mit Fokus Akustik" und "Testingenieur:in mit Fokus EMV" bei Stadler Rail als
#: dieselbe Stelle galten — zwei verschiedene Ausschreibungen.
#:
#: Kalibriert an 29 portalübergreifenden Clustern: die beiden Fehltreffer lagen
#: bei 0.84 und 0.53, die korrekten Treffer bei 0.90 bis 1.00.
MIN_TITLE_SIMILARITY = 0.88

#: Gewichte des portalübergreifenden Scores.
W_TITLE = 0.45
W_COMPANY = 0.35
W_LOCATION = 0.20

#: Jaccard-Schwelle für Near-Duplicates innerhalb derselben Firma.
NEAR_DUP_THRESHOLD = 0.85

#: Grösse der Wort-Shingles fürs MinHash-artige Verfahren.
SHINGLE_SIZE = 5

_WORD_RE = re.compile(r"\w+", re.UNICODE)

#: Rangfolge der kanonischen Quelle. job-room gewinnt: reichere Felder,
#: Koordinaten, Ablaufdatum.
PORTAL_RANK = {"job_room": 0, "myjob": 1, "ostjob": 2, "zentraljob": 3}


# --------------------------------------------------------------------------
# Portalübergreifend
# --------------------------------------------------------------------------


def _apply_url_key(url: str | None) -> str | None:
    """Host + Pfad einer Bewerbungs-URL, ohne Tracking-Parameter."""
    if not url:
        return None
    try:
        parts = urlparse(url)
    except ValueError:
        return None
    if not parts.netloc:
        return None
    host = parts.netloc.lower().removeprefix("www.")
    path = parts.path.rstrip("/").lower()
    return f"{host}{path}" if path else host


def title_similarity(a: JobPosting, b: JobPosting) -> float:
    """Wie ähnlich sind die Stellentitel?

    ``token_sort_ratio`` statt ``token_set_ratio``: letzteres ignoriert
    überzählige Tokens und hält damit "Development Engineer Sensor Protection"
    und "Software Development Engineer" für nahezu gleich.
    """
    return float(fuzz.token_sort_ratio(a.title_key, b.title_key)) / 100.0


def similarity(a: JobPosting, b: JobPosting) -> float:
    """Gewichteter Ähnlichkeitsscore zwischen 0 und 1."""
    title = title_similarity(a, b)
    company = fuzz.token_set_ratio(a.company_key, b.company_key) / 100.0
    same_place = bool(
        (a.postal_code and a.postal_code == b.postal_code)
        or (a.city and b.city and a.city.lower() == b.city.lower())
    )
    return W_TITLE * title + W_COMPANY * company + W_LOCATION * (1.0 if same_place else 0.0)


def is_same_job(a: JobPosting, b: JobPosting) -> tuple[bool, str]:
    """Sind das zwei Ausschreibungen derselben Stelle?

    Gibt zusätzlich zurück, welches Signal entschieden hat — hilfreich beim
    Kalibrieren der Schwelle.
    """
    if a.portal == b.portal and a.source_id == b.source_id:
        return True, "identisch"

    key_a, key_b = _apply_url_key(a.apply_url), _apply_url_key(b.apply_url)
    if key_a and key_a == key_b:
        # Beide zeigen auf dieselbe Bewerbungsseite beim Arbeitgeber. Das ist
        # praktisch ein Beweis und schlägt die Fuzzy-Schwelle.
        return True, "apply_url"

    # Der Titel entscheidet zuerst. Firma und Ort dürfen ihn nicht überstimmen.
    t = title_similarity(a, b)
    if t < MIN_TITLE_SIMILARITY:
        return False, f"titel={t:.2f} unter {MIN_TITLE_SIMILARITY}"

    score = similarity(a, b)
    return (score >= CROSS_PORTAL_THRESHOLD), f"fuzzy={score:.2f}"


@dataclass
class Cluster:
    """Eine Stelle, gesehen auf einem oder mehreren Portalen."""

    cluster_id: str
    members: list[JobPosting] = field(default_factory=list)

    @property
    def canonical(self) -> JobPosting:
        """Die Fassung mit den reichsten Feldern."""
        return min(self.members, key=lambda j: (PORTAL_RANK.get(j.portal, 99), j.source_id))

    @property
    def portals(self) -> set[str]:
        return {m.portal for m in self.members}


def _cluster_id(job: JobPosting) -> str:
    basis = f"{job.title_key}|{job.company_key}|{job.dedup_block}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def cluster_across_portals(jobs: Sequence[JobPosting]) -> list[Cluster]:
    """Gruppiert gleiche Stellen über Portale hinweg.

    Blocking auf PLZ beziehungsweise Ort hält den Vergleichsraum klein: ohne
    das wären es bei 20'000 Inseraten 200 Millionen Paarvergleiche.
    """
    blocks: dict[str, list[JobPosting]] = defaultdict(list)
    for job in jobs:
        blocks[job.dedup_block].append(job)

    clusters: list[Cluster] = []
    for block_jobs in blocks.values():
        buckets: list[list[JobPosting]] = []
        for job in block_jobs:
            for bucket in buckets:
                matched, _ = is_same_job(bucket[0], job)
                if matched:
                    bucket.append(job)
                    break
            else:
                buckets.append([job])
        for bucket in buckets:
            cid = _cluster_id(bucket[0])
            clusters.append(Cluster(cluster_id=cid, members=bucket))

    return clusters


# --------------------------------------------------------------------------
# Near-Duplicates innerhalb einer Firma
# --------------------------------------------------------------------------


def shingles(text: str, size: int = SHINGLE_SIZE) -> set[str]:
    """Wort-n-Gramme des bereinigten Textes."""
    words = _WORD_RE.findall(strip_html(text).lower())
    if len(words) < size:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i : i + size]) for i in range(len(words) - size + 1)}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


@dataclass
class CompanyGroup:
    """Mehrfach ausgeschriebene, textgleiche Stelle einer Firma."""

    representative: JobPosting
    duplicates: list[JobPosting] = field(default_factory=list)

    @property
    def size(self) -> int:
        return 1 + len(self.duplicates)

    @property
    def locations(self) -> list[str]:
        seen: list[str] = []
        for job in [self.representative, *self.duplicates]:
            place = job.city or job.postal_code
            if place and place not in seen:
                seen.append(place)
        return seen


def collapse_company_duplicates(
    jobs: Iterable[JobPosting], threshold: float = NEAR_DUP_THRESHOLD
) -> list[CompanyGroup]:
    """Fasst mehrfach ausgeschriebene Stellen derselben Firma zusammen.

    Ein Vermittler, der dieselbe Stelle für 40 Ortschaften ausschreibt, wird zu
    **einem** Eintrag mit einer Ortsliste.

    Warum nicht über Textähnlichkeit
    --------------------------------
    Der naheliegende Ansatz — gleiche Firma plus nahezu gleicher Text — scheitert
    an genau dem Anbieter, der das Problem verursacht. Gemessen an zwei
    MediPersonal-Inseraten mit **identischem Titel und identischem Ort**:
    Jaccard 0.27. Die Texte sind LLM-generiert und jedes Mal neu formuliert
    ("liegt malerisch am Zürichsee" gegen "liegt in unmittelbarer Nähe zu
    Zürich"). Eine Textschwelle, die das einfängt, würde unterschiedliche
    Stellen zusammenwerfen.

    Tragfähig ist stattdessen **Firma + normalisierter Titel**. Zwei
    Ausschreibungen desselben Arbeitgebers für dieselbe Rolle sind dieselbe
    Stelle, egal wie verschieden der Werbetext ausfällt. Der Textvergleich
    bleibt als zweiter Durchgang für Fälle, in denen die Titel leicht
    abweichen.
    """
    by_company: dict[str, list[JobPosting]] = defaultdict(list)
    for job in jobs:
        by_company[job.company_key].append(job)

    groups: list[CompanyGroup] = []
    for company_jobs in by_company.values():
        # Stufe 1: Firma + normalisierter Titel. Exakt, keine Schwelle nötig.
        by_title: dict[str, list[JobPosting]] = defaultdict(list)
        for job in company_jobs:
            by_title[job.title_key].append(job)

        buckets: list[tuple[JobPosting, list[JobPosting], set[str]]] = []
        for same_title in by_title.values():
            head = min(same_title, key=lambda j: (PORTAL_RANK.get(j.portal, 99), j.source_id))
            rest = [j for j in same_title if j is not head]
            buckets.append((head, rest, shingles(head.description_md)))

        # Stufe 2: Titel weichen ab, Text ist aber praktisch identisch.
        merged: list[tuple[JobPosting, list[JobPosting], set[str]]] = []
        for head, rest, sh in buckets:
            for _m_head, m_rest, m_sh in merged:
                if jaccard(sh, m_sh) >= threshold:
                    m_rest.append(head)
                    m_rest.extend(rest)
                    break
            else:
                merged.append((head, list(rest), sh))

        groups.extend(CompanyGroup(representative=h, duplicates=r) for h, r, _ in merged)

    return groups


def dedup_stats(groups: Sequence[CompanyGroup]) -> dict[str, int | float]:
    total = sum(g.size for g in groups)
    collapsed = len(groups)
    biggest = max((g.size for g in groups), default=0)
    return {
        "inserate_vorher": total,
        "stellen_nachher": collapsed,
        "entfernt": total - collapsed,
        "groesste_gruppe": biggest,
        "reduktion_prozent": round(100 * (total - collapsed) / total, 1) if total else 0.0,
    }
