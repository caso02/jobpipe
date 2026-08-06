"""Erkennt Personalvermittler.

Warum nicht einfach ``company.surrogate``
-----------------------------------------
job-room hat ein Feld dafür — es ist eine **Selbstdeklaration und damit
unzuverlässig**. Gemessen am echten Bestand setzen Yellowshark, Randstad,
job impuls AG, Excellent Personaldienstleistungen, Alegro Personal, G&L Partner
und Trabeco durchweg ``surrogate = False``, obwohl alle Vermittler sind. Der
darauf gestützte Malus im Scoring griff bei diesen Firmen schlicht nie.

Das ist kein Randproblem: rund die Hälfte des Bestands stammt von Vermittlern,
und wer sich nicht selbst deklariert, rutscht ungebremst nach oben.

Das Ersatzsignal
----------------
Vier Prüfungen in fester Reihenfolge:

1. **Firmenname** — "Personal", "Recruiting", "Temporär" und Verwandte. Zuerst,
   damit eine "Klinik Personal AG" nicht über Schritt 2 als Spital durchgeht.
2. **Öffentliche Grossarbeitgeber** (Kanton, Stadt, Spital, Schule …) sind nie
   Vermittler. Diese Regel schützt sie vor Schritt 4.
3. **Selbstdeklaration** ``surrogate``. Verlässlich, wenn gesetzt — unzuverlässig
   ist nur ihr Fehlen.
4. **Verhaltensmuster in den Daten**: viele verschiedene AVAM-Berufscodes *und*
   räumlich gestreute Arbeitsorte.

Punkt 4 ist der eigentliche Fortschritt. Entscheidend ist dabei nicht die
Berufsvielfalt allein — ein Kanton schreibt ebenfalls über zwanzig Berufsarten
aus —, sondern deren Kombination mit der **Ortsstreuung**:

===========================  ======  =====  ==========
Firma                        Codes   Orte   Streuung
===========================  ======  =====  ==========
Kanton Zürich                    21      1      0.0 km
Stadt Zürich                     15      1      0.5 km
Yellowshark                      20     17     23.1 km
Jobup                            75     51     22.4 km
===========================  ======  =====  ==========

Ein Kanton stellt in seinen eigenen Amtsstellen an, ein Vermittler platziert
bei Kunden in der ganzen Region.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from jobpipe.parse.geo import haversine_km
from jobpipe.parse.schema import JobPosting, strip_html

#: Öffentliche und institutionelle Arbeitgeber. Sie schreiben breit aus, aber
#: sie vermitteln nicht. Diese Prüfung hat Vorrang vor allen anderen.
#:
#: Kein abschliessendes ``\b``: Deutsch bildet Komposita, und mit Wortgrenze
#: am Ende fielen "Universitätsspital" und "Kantonsspital" durch das Raster.
PUBLIC_EMPLOYER_RE = re.compile(
    r"\b(kanton|stadt|gemeinde|bund|bundesamt|"
    r"spital|klinik|universit|hochschule|schule|kindergarten|"
    r"pflegezentrum|alterszentrum|museum|bibliothek|polizei|feuerwehr|"
    r"post ch|sbb|swisscom|migros|coop)",
    re.IGNORECASE,
)

#: Namensbestandteile, die auf Personalvermittlung hindeuten.
AGENCY_NAME_RE = re.compile(
    r"(personaldienstleist|personalberatung|personalvermittl|stellenvermittl|"
    r"personal\b|recruit|staffing|temporär|temporar|interim|human capital|"
    r"human resources|hr\s*services|manpower|adecco|randstad|hays|yellowshark|"
    r"universal-job|job impuls|jobup|workmanagement|work selection|work4you|"
    r"fachkraft\.ch|arbeitsvermittl)",
    re.IGNORECASE,
)

#: Ab so vielen Inseraten lohnt die Musteranalyse.
#:
#: Kalibriert an echten Fehltreffern: mit einer Schwelle von 10 galten Stadler
#: Rail (15 Berufscodes bei 19 Inseraten, 6 Standorte) und Raiffeisen Schweiz
#: (11 Codes bei 17 Inseraten, 10 Orte) als Vermittler. Beide sind Arbeitgeber
#: mit echten, verteilten Standorten. Unter rund 25 Inseraten ist die
#: Stichprobe zu klein, um sie von einem Vermittler zu unterscheiden — dort
#: entscheidet nur der Firmenname.
MIN_ADS_FOR_PATTERN = 25

#: Anteil verschiedener AVAM-Berufscodes an den Inseraten.
MIN_CODE_RATIO = 0.30

#: Räumliche Streuung: entweder viele verschiedene Orte …
MIN_DISTINCT_CITIES = 8

#: … oder eine hohe mittlere Entfernung vom Schwerpunkt.
MIN_SPREAD_KM = 5.0

#: Wie viele Inserate einer Firma eine Auftraggeberfirma erwähnen müssen.
#:
#: Absolut statt anteilig, weil der Anteil bei den grossen Vermittlern niedrig
#: sein kann (iPersonal 9 % von 1'490 Inseraten) und bei den kleinen hoch.
#: Drei Nennungen sind kein Zufall.
MIN_CLIENT_MENTIONS = 3

#: Wendungen, mit denen eine Vermittlung ihre Auftraggeberfirma benennt.
#:
#: Bewusst nur solche, die eine **Firma** meinen. Ein blosses "für unsere
#: Kunden" ist Dienstleistungssprache und steht bei der Post so gut wie bei
#: jeder Bank — damit lag der Anteil bei Post CH AG bei 30 % statt 0 %.
CLIENT_MENTION_RE = re.compile(
    r"für\s+(?:unseren|einen)\s+"
    r"(?:langjährigen\s+|renommierten\s+|geschätzten\s+)?"
    r"(?:Kunden|Auftraggeber|Mandanten)\b"
    r"|im\s+Auftrag\s+(?:unseres|einer|eines)\b"
    r"|unser(?:e)?\s+Kund(?:e|in)\s+ist\s+(?:ein|eine)\b"
    r"|vermittelt\s+diese\s+Stelle"
    r"|für\s+(?:unsere|eine)\s+Kundin\b"
    r"|für\s+ein\s+(?:renommiertes\s+|erfolgreiches\s+)?Unternehmen\s+in\b",
    re.IGNORECASE,
)


@dataclass
class CompanyProfile:
    """Aggregierte Kennzahlen einer Firma über alle ihre Inserate."""

    name: str
    ads: int = 0
    codes: set[str] = field(default_factory=set)
    cities: set[str] = field(default_factory=set)
    points: list[tuple[float, float]] = field(default_factory=list)
    declared_agency: bool = False
    #: Inserate, die eine Auftraggeberfirma erwähnen.
    client_mentions: int = 0

    @property
    def code_ratio(self) -> float:
        return len(self.codes) / self.ads if self.ads else 0.0

    @property
    def spread_km(self) -> float:
        """Mittlere Entfernung der Arbeitsorte vom Schwerpunkt."""
        if len(self.points) < 2:
            return 0.0
        clat = statistics.fmean(p[0] for p in self.points)
        clon = statistics.fmean(p[1] for p in self.points)
        return statistics.fmean(haversine_km(clat, clon, p[0], p[1]) for p in self.points)


@dataclass
class Verdict:
    is_agency: bool
    reason: str


def is_public_employer(name: str) -> bool:
    return bool(PUBLIC_EMPLOYER_RE.search(name))


def name_suggests_agency(name: str) -> bool:
    return bool(AGENCY_NAME_RE.search(name))


def matches_pattern(profile: CompanyProfile) -> bool:
    """Viele Berufsarten, räumlich gestreut — UND im Text als Vermittlung
    erkennbar.

    Die ersten beiden Bedingungen allein reichen nicht. Gemessen an echten
    Firmen liegen *Stadler Rail Management AG* (0.42 Codes je Inserat, 6 Orte)
    und *Raiffeisen Schweiz* (0.50, 10 Orte) mitten im Wertebereich echter
    Vermittler (0.03 bis 0.83) — ein Grossbetrieb mit vielen Standorten und
    breitem Stellenangebot sieht statistisch aus wie eine Vermittlung. Beide
    wurden deshalb falsch etikettiert.

    Der dritte Test ist der zuverlässige: **eine Vermittlung sagt es im
    Text.** "Für unseren Kunden suchen wir", "im Auftrag unseres
    Auftraggebers", "iPersonal vermittelt diese Stelle". Gemessen:

    ============================  =======
    Firma                         Anteil
    ============================  =======
    Work Selection                    62%
    Workmanagement AG                 38%
    Randstad                          32%
    iPersonal AG                       9%
    Stadler Rail Management AG         0%
    Raiffeisen Schweiz                 0%
    Post CH AG                         0%
    ============================  =======

    Bewusst nur die Wendungen, die eine **Auftraggeberfirma** meinen. Ein
    blosses "für unsere Kunden" ist Dienstleistungssprache und steht bei der
    Post so gut wie bei jeder Bank.
    """
    if profile.ads < MIN_ADS_FOR_PATTERN:
        return False
    if profile.code_ratio < MIN_CODE_RATIO:
        return False
    if profile.client_mentions < MIN_CLIENT_MENTIONS:
        return False
    return len(profile.cities) >= MIN_DISTINCT_CITIES or profile.spread_km >= MIN_SPREAD_KM


def classify(profile: CompanyProfile) -> Verdict:
    """Entscheidet für eine Firma. Die Reihenfolge der Prüfungen ist bewusst."""
    if name_suggests_agency(profile.name):
        # Vor der Prüfung auf öffentliche Arbeitgeber: sonst würde eine
        # "Klinik Personal AG" als Spital durchgehen. Kein echter öffentlicher
        # Arbeitgeber trägt "Personal", "Recruiting" oder "Temporär" im Namen.
        return Verdict(True, "Firmenname")
    if is_public_employer(profile.name):
        # Danach, damit ein breit ausschreibender Kanton nicht durch die
        # Musteranalyse fällt.
        return Verdict(False, "öffentlicher Arbeitgeber")
    if profile.declared_agency:
        return Verdict(True, "Selbstdeklaration (surrogate)")
    if matches_pattern(profile):
        return Verdict(
            True,
            f"Muster: {len(profile.codes)} Berufscodes bei {profile.ads} Inseraten, "
            f"{len(profile.cities)} Orte, Streuung {profile.spread_km:.0f} km",
        )
    return Verdict(False, "")


def build_profiles(jobs: Iterable[JobPosting]) -> dict[str, CompanyProfile]:
    """Fasst Inserate je Firma zusammen.

    Gruppiert über den normalisierten Firmennamen, damit "Stadler Rail AG" und
    "Stadler Rail" nicht getrennt gezählt werden.
    """
    profiles: dict[str, CompanyProfile] = {}
    for job in jobs:
        key = job.company_key
        p = profiles.get(key)
        if p is None:
            p = CompanyProfile(name=job.company_name)
            profiles[key] = p
        p.ads += 1
        p.codes.update(job.occupation_codes)
        if job.city:
            p.cities.add(job.city)
        if job.lat is not None and job.lon is not None:
            p.points.append((job.lat, job.lon))
        if job.company_is_agency:
            p.declared_agency = True
        if CLIENT_MENTION_RE.search(strip_html(job.description_md)):
            p.client_mentions += 1
    return profiles


def annotate(jobs: Sequence[JobPosting]) -> dict[str, Verdict]:
    """Setzt ``company_is_agency`` auf allen Inseraten neu.

    Gibt die Urteile je Firmenschlüssel zurück — nützlich für Diagnose und
    Tests.
    """
    profiles = build_profiles(jobs)
    verdicts = {key: classify(p) for key, p in profiles.items()}
    for job in jobs:
        v = verdicts.get(job.company_key)
        if v is not None:
            job.company_is_agency = v.is_agency
            job.agency_reason = v.reason
    return verdicts


def summary(jobs: Sequence[JobPosting], verdicts: dict[str, Verdict]) -> dict[str, object]:
    agencies = sum(1 for j in jobs if j.company_is_agency)
    by_reason: dict[str, int] = {}
    for v in verdicts.values():
        if v.is_agency:
            signal = v.reason.split(":")[0]
            by_reason[signal] = by_reason.get(signal, 0) + 1
    return {
        "inserate_von_vermittlern": agencies,
        "anteil_prozent": round(100 * agencies / len(jobs), 1) if jobs else 0.0,
        "firmen_als_vermittler": sum(1 for v in verdicts.values() if v.is_agency),
        "nach_signal": by_reason,
    }
