"""Ortsauflösung und Distanzen.

job-room liefert Koordinaten mit, die CH-Media-Portale nur PLZ und Ort. Die
Nachschlagetabelle stammt aus den job-room-Rohdaten selbst (siehe
:mod:`jobpipe.store.reference`) — kein externer Datensatz nötig.
"""

from __future__ import annotations

import math
import sqlite3

from jobpipe.parse.schema import JobPosting

EARTH_RADIUS_KM = 6371.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Grosskreisdistanz in Kilometern."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


class Geocoder:
    """PLZ/Ort -> Koordinaten, mit In-Memory-Cache.

    Lädt die Tabelle einmal komplett; bei ein paar hundert Ortschaften ist das
    günstiger als eine Abfrage pro Inserat.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._by_plz_city: dict[tuple[str, str], tuple[float, float, str | None]] = {}
        self._by_plz: dict[str, tuple[float, float, str | None]] = {}
        self._by_city: dict[str, tuple[float, float, str | None]] = {}
        for row in conn.execute("SELECT postal_code, city, canton, lat, lon FROM plz_coords"):
            plz = str(row["postal_code"])
            city = str(row["city"])
            entry = (float(row["lat"]), float(row["lon"]), row["canton"])
            self._by_plz_city[(plz, city.lower())] = entry
            self._by_plz.setdefault(plz, entry)
            self._by_city.setdefault(city.lower(), entry)

    def __len__(self) -> int:
        return len(self._by_plz_city)

    def lookup(
        self, postal_code: str | None, city: str | None
    ) -> tuple[float, float, str | None] | None:
        """Genaueste zuerst: PLZ+Ort, dann PLZ, dann Ort."""
        plz = (postal_code or "").strip()
        town = (city or "").strip().lower()
        if plz and town and (plz, town) in self._by_plz_city:
            return self._by_plz_city[(plz, town)]
        if plz and plz in self._by_plz:
            return self._by_plz[plz]
        if town and town in self._by_city:
            return self._by_city[town]
        return None

    def enrich(self, job: JobPosting) -> JobPosting:
        """Ergänzt Koordinaten und Kanton, wenn sie fehlen."""
        if job.lat is not None and job.lon is not None and job.canton:
            return job
        hit = self.lookup(job.postal_code, job.city)
        if not hit:
            return job
        lat, lon, canton = hit
        if job.lat is None:
            job.lat = lat
        if job.lon is None:
            job.lon = lon
        if not job.canton and canton:
            job.canton = canton
        return job


def distance_to(job: JobPosting, lat: float, lon: float) -> float | None:
    if job.lat is None or job.lon is None:
        return None
    return haversine_km(job.lat, job.lon, lat, lon)
