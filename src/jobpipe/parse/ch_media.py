"""Rohdaten der CH-Media-Portale -> ``JobPosting``.

Quelle ist ``vacancyDetails.data`` aus ``window.__PRELOADED_STATE__``.
Feldnamen sind bei myjob.ch, ostjob.ch und zentraljob.ch identisch.

Zwei Eigenheiten gegenüber job-room:

* **Keine Koordinaten.** Nur ``workplace_zip`` und ``workplace_city``; die
  Auflösung übernimmt :mod:`jobpipe.parse.geo`.
* **iframe-Inserate.** Ein Teil der Ads bettet die Beschreibung von der
  Arbeitgeber-Domain ein; auf dem Portal steht dann nur eine Kurzfassung. Der
  iframe wird **nicht** verfolgt (fremde Domain, eigene robots.txt und AGB) —
  stattdessen wird ``description_truncated`` gesetzt, damit das Ranking den
  schwächeren Text nicht für ein schlechtes Inserat hält.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import structlog

from jobpipe.parse.schema import JobPosting, strip_html

log = structlog.get_logger(__name__)

#: Unterhalb dieser Zeichenzahl gilt die Beschreibung als abgeschnitten.
TRUNCATION_THRESHOLD = 350

#: Firmennamen, die auf einen Personalvermittler hindeuten. CH Media liefert
#: kein ``surrogate``-Flag wie job-room, deshalb heuristisch.
AGENCY_HINTS = (
    "personal",
    "temporär",
    "temporar",
    "recruit",
    "staffing",
    "interim",
    "vermittlung",
    "stellenvermittlung",
    "job ag",
    "manpower",
    "adecco",
    "randstad",
    "universal-job",
)


def _to_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _to_datetime(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    for candidate in (text, text[:19], text[:10]):
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            continue
    return None


def looks_like_agency(company_name: str) -> bool:
    lowered = company_name.lower()
    return any(hint in lowered for hint in AGENCY_HINTS)


def _same_host(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    try:
        return urlparse(a).netloc.lower() == urlparse(b).netloc.lower()
    except ValueError:
        return False


def parse(
    raw: dict[str, Any],
    portal: str,
    raw_path: Path | str | None = None,
) -> JobPosting | None:
    source_id = raw.get("id")
    title = (raw.get("title") or "").strip()
    company = raw.get("company") or {}
    company_name = (company.get("name") or "").strip()
    if not source_id or not title or not company_name:
        return None

    activity = raw.get("activity") or ""
    requirements = raw.get("requirements") or ""
    description = "\n\n".join(part for part in (activity, requirements) if part).strip()

    # Zeigt url_description auf eine fremde Domain, steckt der eigentliche Text
    # in einem iframe dort — wir folgen ihm nicht.
    url_description = raw.get("url_description")
    source_url = raw.get("_source_url") or ""
    external_body = bool(url_description) and not _same_host(url_description, source_url)
    truncated = external_body or len(strip_html(description)) < TRUNCATION_THRESHOLD

    return JobPosting(
        portal=portal,
        source_id=str(source_id),
        source_url=source_url or f"https://www.{portal}.ch/job/x/{source_id}",
        title=title,
        company_name=company_name,
        company_is_agency=looks_like_agency(company_name),
        description_md=description,
        description_truncated=truncated,
        city=(raw.get("workplace_city") or "").strip() or None,
        postal_code=(raw.get("workplace_zip") or "").strip() or None,
        canton=None,  # wird über die PLZ aufgelöst
        lat=None,
        lon=None,
        workload_min=_to_int(raw.get("type_value_min")),
        workload_max=_to_int(raw.get("type_value_max")),
        is_permanent=None,
        start_date=(_to_datetime(raw.get("date_start")) or datetime.min).date()
        if raw.get("date_start")
        else None,
        # Einziges Portal mit explizitem Homeoffice-Feld.
        home_office=raw.get("home_office") if isinstance(raw.get("home_office"), bool) else None,
        posted_at=_to_datetime(raw.get("date_first_published"))
        or _to_datetime(raw.get("date_actualization")),
        expires_at=None,  # CH Media nennt kein Ablaufdatum; 410 Gone entscheidet
        status="active",
        apply_url=raw.get("url_application") or url_description,
        apply_via_email=bool(raw.get("has_contact_person")),
        apply_via_phone=False,
        occupation_codes=[],
        categories=[
            str(c.get("title"))
            for c in (raw.get("categories") or [])
            if isinstance(c, dict) and c.get("title")
        ],
        languages=[],
        raw_path=str(raw_path) if raw_path else None,
    )
