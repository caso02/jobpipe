"""Rohdaten von job-room.ch -> ``JobPosting``.

Arbeitet ausschliesslich auf den bereits abgelegten, bereinigten Rohdaten unter
``data/raw/job_room/`` — nie gegen das Netz.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import structlog

from jobpipe.parse.schema import JobPosting

log = structlog.get_logger(__name__)

PORTAL = "job_room"
DETAIL_URL = "https://www.job-room.ch/job-search/{id}"


def _to_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_datetime(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    try:  # reines Datum
        return datetime.combine(date.fromisoformat(text[:10]), datetime.min.time())
    except ValueError:
        return None


def _pick_description(descriptions: list[Any]) -> tuple[str, str]:
    """Bevorzugt Deutsch, fällt sonst auf die erste Sprache zurück."""
    best: dict[str, Any] | None = None
    for d in descriptions:
        if not isinstance(d, dict):
            continue
        if (d.get("languageIsoCode") or "").lower() == "de":
            best = d
            break
        if best is None:
            best = d
    if not best:
        return "", ""
    return (best.get("title") or "").strip(), (best.get("description") or "").strip()


def parse(raw: dict[str, Any], raw_path: Path | str | None = None) -> JobPosting | None:
    """Baut ein ``JobPosting``. Gibt ``None`` bei unbrauchbaren Datensätzen zurück."""
    source_id = raw.get("id")
    content = raw.get("jobContent")
    if not source_id or not isinstance(content, dict):
        return None

    title, description = _pick_description(content.get("jobDescriptions") or [])
    company = content.get("company") or {}
    company_name = (company.get("name") or "").strip()
    if not title or not company_name:
        return None

    location = content.get("location") or {}
    coords = location.get("coordinates") or {}
    employment = content.get("employment") or {}
    flags = content.get("applyChannelFlags") or {}
    publication = raw.get("publication") or {}

    expires_at = _to_datetime(publication.get("endDate"))
    status = "expired" if expires_at and expires_at < datetime.now() else "active"

    return JobPosting(
        portal=PORTAL,
        source_id=str(source_id),
        source_url=DETAIL_URL.format(id=source_id),
        title=title,
        company_name=company_name,
        # job-room markiert Personalvermittler selbst. Bei rund der Hälfte
        # der Inserate ist das gesetzt — entscheidend fürs Ranking.
        company_is_agency=bool(company.get("surrogate")),
        description_md=description,
        description_truncated=False,
        city=(location.get("city") or "").strip() or None,
        postal_code=(location.get("postalCode") or "").strip() or None,
        canton=(location.get("cantonCode") or "").strip() or None,
        lat=_to_float(coords.get("lat")),
        lon=_to_float(coords.get("lon")),
        workload_min=_to_int(employment.get("workloadPercentageMin")),
        workload_max=_to_int(employment.get("workloadPercentageMax")),
        is_permanent=employment.get("permanent")
        if isinstance(employment.get("permanent"), bool)
        else None,
        start_date=(_to_datetime(employment.get("startDate")) or datetime.min).date()
        if employment.get("startDate")
        else None,
        home_office=_home_office(employment),
        posted_at=_to_datetime(publication.get("startDate"))
        or _to_datetime(raw.get("createdTime")),
        expires_at=expires_at,
        status=status,
        apply_url=flags.get("form_url") or content.get("externalUrl"),
        apply_via_email=bool(flags.get("apply_via_email")),
        apply_via_phone=bool(flags.get("apply_via_phone")),
        occupation_codes=[
            str(o.get("avamOccupationCode"))
            for o in (content.get("occupations") or [])
            if isinstance(o, dict) and o.get("avamOccupationCode")
        ],
        categories=[],
        languages=[
            str(lang.get("languageIsoCode"))
            for lang in (content.get("languageSkills") or [])
            if isinstance(lang, dict) and lang.get("languageIsoCode")
        ],
        raw_path=str(raw_path) if raw_path else None,
    )


def _home_office(employment: dict[str, Any]) -> bool | None:
    """job-room hat kein Homeoffice-Feld, nur ``workForms``.

    In 1'000 gemessenen Inseraten war ``HOME_WORK`` genau einmal gesetzt —
    das Feld ist praktisch wertlos. ``None`` heisst darum "unbekannt", nicht
    "nein"; die Textsuche im Scoring entscheidet.
    """
    forms = employment.get("workForms")
    if not isinstance(forms, list) or not forms:
        return None
    return True if "HOME_WORK" in forms else None
