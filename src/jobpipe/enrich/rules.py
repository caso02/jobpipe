"""Regelbasierte Kriterien.

Jede Regel liefert einen Wert zwischen 0 und 1 plus eine kurze Begründung im
Klartext. Die Begründungen landen im Digest — ein Ranking, das man nicht
nachvollziehen kann, benutzt man nach einer Woche nicht mehr.

Zwei Regeln verdienen besondere Aufmerksamkeit:

* **Gleitzeit** ist für Profil B ein Hauptkriterium, steht aber nur in 1.2 %
  der Inserate (gemessen an 1'000 Stück; das strukturierte Feld ``workForms``
  war genau einmal gefüllt). Deshalb ausschliesslich Bonus, nie Filter.
* **Ausschluss-Keywords** wirken negativ und werden im Titel schwerer gewichtet
  als im Fliesstext: "Empfangsmitarbeiterin" im Titel ist eine Aussage über die
  Stelle, "Empfang" irgendwo im Beschreibungstext oft nur eine Randnotiz.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from jobpipe.config import Profile
from jobpipe.enrich import seniority as sen
from jobpipe.parse.geo import haversine_km
from jobpipe.parse.schema import strip_html

#: Formulierungen, die auf Gleitzeit hindeuten.
FLEXTIME_RE = re.compile(
    r"gleitzeit|gleitende\s+arbeitszeit|jahresarbeitszeit|flexible[nrs]?\s+arbeitszeit"
    r"|zeitautonom|vertrauensarbeitszeit",
    re.IGNORECASE,
)

#: Formulierungen, die auf Homeoffice hindeuten.
HOME_OFFICE_RE = re.compile(
    r"home\s?-?office|homeoffice|remote|telearbeit|mobiles?\s+arbeiten|hybrid",
    re.IGNORECASE,
)

#: Halbwertszeit der Aktualität in Tagen.
RECENCY_HALFLIFE_DAYS = 21.0


@dataclass
class RuleResult:
    scores: dict[str, float] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)
    distance_km: float | None = None

    def add(self, name: str, value: float, reason: str) -> None:
        self.scores[name] = round(value, 4)
        if reason:
            self.reasons[name] = reason

    def as_json(self) -> dict[str, Any]:
        return {"scores": self.scores, "reasons": self.reasons}


# --------------------------------------------------------------------------
# Einzelne Regeln
# --------------------------------------------------------------------------


def score_workload(job: Any, profile: Profile) -> tuple[float, str]:
    """Überlappung des Pensums mit dem Wunschbereich."""
    lo = job.workload_min if job.workload_min is not None else 0
    hi = job.workload_max if job.workload_max is not None else 100
    if job.workload_min is None and job.workload_max is None:
        return 0.5, "Pensum nicht angegeben"

    overlap = min(hi, profile.workload_max) - max(lo, profile.workload_min)
    if overlap < 0:
        return 0.0, f"Pensum {lo}-{hi}% ausserhalb {profile.workload_min}-{profile.workload_max}%"
    wanted = max(1, profile.workload_max - profile.workload_min)
    return min(1.0, (overlap + 1) / (wanted + 1)), f"Pensum {lo}-{hi}%"


def score_location(job: Any, profile: Profile) -> tuple[float, str, float | None]:
    """Weiche Abstufung statt hartem Radius-Schnitt.

    Innerhalb des Radius voller Wert, danach linear abfallend bis zum
    doppelten Radius. Ein Inserat 2 km ausserhalb ist keine schlechtere
    Stelle als eines 2 km innerhalb.
    """
    if job.lat is None or job.lon is None:
        return 0.4, "Ort unbekannt", None
    best: tuple[float, str, float] | None = None
    for loc in profile.locations:
        if loc.lat is None or loc.lon is None:
            continue
        d = haversine_km(job.lat, job.lon, loc.lat, loc.lon)
        if d <= loc.radius_km:
            value = 1.0
        elif d <= loc.radius_km * 2:
            value = 1.0 - (d - loc.radius_km) / loc.radius_km
        else:
            value = 0.0
        if best is None or value > best[0]:
            best = (value, f"{d:.0f} km von {loc.label}", d)
    if best is None:
        return 0.4, "kein Referenzort im Profil", None
    return best[0], best[1], best[2]


def score_flextime(job: Any, profile: Profile) -> tuple[float, str]:
    """Nur Bonus, nie Filter.

    Gemessen steht "Gleitzeit" in 1.2 % der Inserate. Ein harter Filter würde
    98 % wegwerfen, die allermeisten davon zu Unrecht — die Frage gehört ins
    Vorstellungsgespräch, nicht in den Filter.
    """
    if not profile.wants_flextime:
        return 0.5, ""
    haystack = f"{job.title} {strip_html(job.description_md)}"
    match = FLEXTIME_RE.search(haystack)
    if match:
        return 1.0, f"Gleitzeit erwähnt: «{_quote(haystack, match)}»"
    return 0.35, "keine Angabe zur Arbeitszeit"


def score_home_office(job: Any, profile: Profile) -> tuple[float, str]:
    if not profile.wants_home_office:
        return 0.5, ""
    if job.home_office is True:
        return 1.0, "Homeoffice laut Portal"
    haystack = f"{job.title} {strip_html(job.description_md)}"
    match = HOME_OFFICE_RE.search(haystack)
    if match:
        return 0.9, f"Homeoffice erwähnt: «{_quote(haystack, match)}»"
    return 0.35, "keine Angabe zu Homeoffice"


def score_keywords(job: Any, profile: Profile) -> tuple[float, str]:
    """Positiv-Signale, Titel zählt doppelt."""
    if not profile.keywords_positive:
        return 0.5, ""
    title = job.title.lower()
    body = strip_html(job.description_md).lower()
    in_title = [k for k in profile.keywords_positive if k.lower() in title]
    in_body = [k for k in profile.keywords_positive if k.lower() in body and k not in in_title]
    if not in_title and not in_body:
        return 0.0, "kein Positiv-Stichwort"
    # Sättigung: der fünfte Treffer sagt weniger aus als der erste.
    raw = 2.0 * len(in_title) + 1.0 * len(in_body)
    value = 1.0 - math.exp(-raw / 3.0)
    hits = in_title + in_body
    return value, "Stichworte: " + ", ".join(hits[:5]) + ("…" if len(hits) > 5 else "")


def score_exclusions(job: Any, profile: Profile) -> tuple[float, str]:
    """Negativ-Signale. Rückgabe ist die STRAFE, nicht die Güte.

    Der Titel wiegt schwerer als der Fliesstext: "Empfangsmitarbeiterin" im
    Titel beschreibt die Stelle, "Empfang" irgendwo im Text ist oft eine
    Randbemerkung ("gelegentliche Aushilfe am Empfang").
    """
    if not profile.keywords_exclude:
        return 0.0, ""
    title = job.title.lower()
    body = strip_html(job.description_md).lower()
    in_title = [k for k in profile.keywords_exclude if k.lower() in title]
    in_body = [k for k in profile.keywords_exclude if k.lower() in body and k not in in_title]
    if not in_title and not in_body:
        return 0.0, ""
    penalty = min(1.0, 1.0 * len(in_title) + 0.25 * len(in_body))
    where = "Titel" if in_title else "Text"
    hits = in_title + in_body
    return penalty, f"Ausschluss im {where}: " + ", ".join(hits[:4])


def score_seniority(job: Any, profile: Profile) -> tuple[float, str]:
    """Rückgabe ist die STRAFE für ein zu hohes Anforderungsniveau.

    Kein harter Ausschluss: eine zu hoch gehängte Stelle ist nicht unmöglich,
    nur unwahrscheinlicher. Ob man sich trotzdem bewirbt, entscheidet der
    Mensch — nicht der Filter.
    """
    s = profile.seniority
    req = sen.detect(job.title, job.description_md)
    return sen.penalty(
        req,
        own_education=s.education,
        own_years=s.years_experience,
        accept_leadership=s.accept_leadership,
        accept_senior=s.accept_senior,
        tolerance_years=s.tolerance_years,
    )


def score_recency(job: Any) -> tuple[float, str]:
    if not job.posted_at:
        return 0.5, ""
    posted = job.posted_at
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=UTC)
    days = max(0.0, (datetime.now(UTC) - posted).total_seconds() / 86400)
    value = 0.5 ** (days / RECENCY_HALFLIFE_DAYS)
    if days < 1:
        return value, "heute publiziert"
    return value, f"vor {days:.0f} Tagen publiziert"


def score_agency(job: Any) -> tuple[float, str]:
    """Rückgabe ist die STRAFE."""
    if job.company_is_agency:
        return 1.0, "Personalvermittler"
    return 0.0, ""


# --------------------------------------------------------------------------


def evaluate(job: Any, profile: Profile) -> RuleResult:
    """Wendet alle Regeln an."""
    res = RuleResult()

    v, why = score_workload(job, profile)
    res.add("workload", v, why)

    v, why, dist = score_location(job, profile)
    res.add("location", v, why)
    res.distance_km = dist

    v, why = score_flextime(job, profile)
    res.add("flextime", v, why)

    v, why = score_home_office(job, profile)
    res.add("home_office", v, why)

    v, why = score_keywords(job, profile)
    res.add("keywords", v, why)

    v, why = score_recency(job)
    res.add("recency", v, why)

    # Strafen, getrennt geführt, damit sie im Digest sichtbar bleiben.
    v, why = score_exclusions(job, profile)
    res.add("exclude_penalty", v, why)

    v, why = score_agency(job)
    res.add("agency_penalty", v, why)

    v, why = score_seniority(job, profile)
    res.add("seniority_penalty", v, why)

    return res


def _quote(text: str, match: re.Match[str], window: int = 45) -> str:
    """Zitiert die Fundstelle mit Kontext, an Wortgrenzen geschnitten.

    Ohne das Einrasten auf Wortgrenzen beginnen die Zitate mitten im Wort
    ("«eiten dank Kauf von zusätzlichen Ferientagen …»") und lesen sich wie
    ein Übertragungsfehler.
    """
    start = max(0, match.start() - window)
    end = min(len(text), match.end() + window)
    if start > 0:
        space = text.find(" ", start)
        if space != -1 and space < match.start():
            start = space + 1
    if end < len(text):
        space = text.rfind(" ", match.end(), end)
        if space != -1:
            end = space
    quoted = re.sub(r"\s+", " ", text[start:end]).strip()
    prefix = "… " if start > 0 else ""
    suffix = " …" if end < len(text) else ""
    return f"{prefix}{quoted}{suffix}"
