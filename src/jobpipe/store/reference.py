"""Referenzdaten: Berufe-Taxonomie und PLZ-Koordinaten.

**Berufe** stammen aus dem Vorprojekt unter ``~/zhaw/DSP`` (berufsberatung.ch,
1'851 Berufe, davon 65 im Berufsfeld Informatik). Sie liefern später lesbare
Cluster-Labels und deutschsprachiges Vokabular für die Skill-Lücken-Analyse.
Beim Import fliegen die Adressblöcke raus — dort stehen Telefonnummern von
Bildungsinstituten.

**PLZ-Koordinaten** werden aus den bereits abgerufenen job-room-Rohdaten
abgeleitet: die API liefert zu jedem Inserat ``postalCode`` *und*
``coordinates``. Das spart einen externen Datensatz, deckt exakt die Region ab,
in der wir suchen, und wächst mit jedem Lauf mit. Für die CH-Media-Portale, die
nur Ort und PLZ liefern, ist das die Auflösungstabelle.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

#: Wo der Datensatz aus dem DSP-Vorprojekt liegt.
DSP_BERUFE_PATH = Path.home() / "zhaw/DSP/cv/swiss-cv-generator/data/CV_DATA.cv_berufsberatung.json"


# --------------------------------------------------------------------------
# Berufe
# --------------------------------------------------------------------------


def reduce_beruf(record: dict[str, Any]) -> dict[str, Any] | None:
    """Reduziert einen berufsberatung-Datensatz auf das Gebrauchte.

    Der Rohdatensatz ist rund 5 KB gross; gebraucht wird ein Bruchteil.
    ``weitere_informationen.adressen`` wird bewusst nicht übernommen — dort
    stehen Telefonnummern von Bildungsinstituten.
    """
    job_id = record.get("job_id")
    title = (record.get("title") or "").strip()
    if not job_id or not title:
        return None

    cats = record.get("categories") or {}
    taet = (record.get("taetigkeiten") or {}).get("kategorien") or {}
    weiter = record.get("weiterbildung") or {}
    info = record.get("weitere_informationen") or {}

    verwandte = [
        {"title": v.get("title"), "job_id": v.get("job_id")}
        for v in (info.get("verwandte_berufe") or [])
        if isinstance(v, dict) and v.get("title")
    ]

    return {
        "job_id": str(job_id),
        "title": title,
        "berufsfeld": cats.get("berufsfelder"),
        "branchen": cats.get("branchen"),
        "swissdoc": cats.get("swissdoc"),
        "bildungstyp": cats.get("bildungstypen"),
        "description": (record.get("description") or "").strip() or None,
        "taetigkeiten": json.dumps(taet, ensure_ascii=False),
        "verwandte_berufe": json.dumps(verwandte, ensure_ascii=False),
        "career_progression": json.dumps(
            weiter.get("career_progression") or [], ensure_ascii=False
        ),
    }


def import_berufe(conn: sqlite3.Connection, source: Path | None = None) -> int:
    """Lädt die Berufe-Taxonomie. Gibt die Zahl importierter Berufe zurück."""
    path = source or DSP_BERUFE_PATH
    if not path.exists():
        raise FileNotFoundError(
            f"Berufsdatensatz nicht gefunden: {path}\n"
            f"Er stammt aus dem DSP-Vorprojekt. Ohne ihn funktioniert die Pipeline, "
            f"nur die Cluster-Labels in M5 bleiben generisch."
        )

    records = json.loads(path.read_text(encoding="utf-8"))
    rows = [r for r in (reduce_beruf(rec) for rec in records) if r]

    conn.executemany(
        """INSERT OR REPLACE INTO berufe
           (job_id, title, berufsfeld, branchen, swissdoc, bildungstyp,
            description, taetigkeiten, verwandte_berufe, career_progression)
           VALUES (:job_id, :title, :berufsfeld, :branchen, :swissdoc, :bildungstyp,
                   :description, :taetigkeiten, :verwandte_berufe, :career_progression)""",
        rows,
    )
    log.info("reference.berufe_imported", count=len(rows), source=str(path))
    return len(rows)


# --------------------------------------------------------------------------
# PLZ-Koordinaten
# --------------------------------------------------------------------------


def _iter_locations(raw_dir: Path) -> Iterator[dict[str, Any]]:
    for f in raw_dir.rglob("*.json.gz"):
        try:
            with gzip.open(f, "rb") as fh:
                ad = json.loads(fh.read().decode("utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        loc = (ad.get("jobContent") or {}).get("location")
        if isinstance(loc, dict):
            yield loc


def build_plz_table(conn: sqlite3.Connection, raw_dir: Path) -> int:
    """Leitet PLZ -> Koordinaten aus den job-room-Rohdaten ab.

    job-room liefert zu jedem Inserat Postleitzahl und Koordinaten. Damit
    entsteht die Auflösungstabelle nebenbei — kein externer Datensatz, keine
    Lizenzfrage, und sie deckt genau die Region ab, in der gesucht wird.
    """
    if not raw_dir.exists():
        return 0

    seen: dict[tuple[str, str], tuple[str, float, float]] = {}
    for loc in _iter_locations(raw_dir):
        plz = (loc.get("postalCode") or "").strip()
        city = (loc.get("city") or "").strip()
        coords = loc.get("coordinates") or {}
        lat, lon = coords.get("lat"), coords.get("lon")
        if not (plz and city and lat and lon):
            continue
        try:
            seen[(plz, city)] = (loc.get("cantonCode") or "", float(lat), float(lon))
        except (TypeError, ValueError):
            continue

    rows = [
        {"postal_code": plz, "city": city, "canton": canton or None, "lat": lat, "lon": lon}
        for (plz, city), (canton, lat, lon) in seen.items()
    ]
    conn.executemany(
        """INSERT OR REPLACE INTO plz_coords (postal_code, city, canton, lat, lon)
           VALUES (:postal_code, :city, :canton, :lat, :lon)""",
        rows,
    )
    log.info("reference.plz_built", count=len(rows))
    return len(rows)


def lookup_coords(
    conn: sqlite3.Connection, postal_code: str, city: str | None = None
) -> tuple[float, float] | None:
    """PLZ (optional mit Ort) -> (lat, lon)."""
    if city:
        row = conn.execute(
            "SELECT lat, lon FROM plz_coords WHERE postal_code = ? AND city = ?",
            (postal_code, city),
        ).fetchone()
        if row:
            return float(row["lat"]), float(row["lon"])
    row = conn.execute(
        "SELECT lat, lon FROM plz_coords WHERE postal_code = ? LIMIT 1", (postal_code,)
    ).fetchone()
    return (float(row["lat"]), float(row["lon"])) if row else None
