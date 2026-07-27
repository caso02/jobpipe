"""Konfiguration laden und validieren.

Zwei getrennte Ebenen, bewusst nicht vermischt:

* ``config.yaml``   — global, eingecheckt, keine Personendaten
* ``profiles/*.yaml`` — pro suchender Person, gitignored (siehe .gitignore)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"
PROFILES_DIR = REPO_ROOT / "profiles"


# --------------------------------------------------------------------------
# Globale Konfiguration
# --------------------------------------------------------------------------


class StorageConfig(BaseModel):
    db_path: Path = Path("data/jobs.db")
    raw_dir: Path = Path("data/raw")

    def resolved_db_path(self) -> Path:
        return _resolve(self.db_path)

    def resolved_raw_dir(self) -> Path:
        return _resolve(self.raw_dir)


class PolitenessConfig(BaseModel):
    """Drosselung und robots.txt-Verhalten.

    Die Defaults sind absichtlich konservativ. Wer sie hochdreht, sollte einen
    Grund haben, der auch gegenüber dem Portalbetreiber Bestand hätte.
    """

    requests_per_second: float = Field(default=1.0, gt=0, le=5.0)
    timeout_seconds: float = Field(default=30.0, gt=0)
    retry_attempts: int = Field(default=3, ge=0, le=10)
    retry_backoff_seconds: float = Field(default=2.0, gt=0)
    user_agent: str = "jobpipe/0.1 (persoenliches Bewerbungsprojekt; +{contact})"
    respect_robots: bool = True
    robots_cache_minutes: int = Field(default=60, ge=0)

    @field_validator("respect_robots")
    @classmethod
    def _warn_on_disable(cls, v: bool) -> bool:
        if not v:
            raise ValueError(
                "respect_robots=false wird nicht unterstützt. Wenn eine Quelle uns "
                "aussperrt, ist das eine Antwort, kein Hindernis."
            )
        return v

    def resolved_user_agent(self) -> str:
        contact = os.environ.get("JOBPIPE_CONTACT_URL", "").strip()
        if not contact:
            contact = "kein-kontakt-konfiguriert"
        return self.user_agent.format(contact=contact)


class RegionConfig(BaseModel):
    cantons: list[str] = Field(default_factory=lambda: ["ZH", "SG", "TG"])

    @field_validator("cantons")
    @classmethod
    def _upper(cls, v: list[str]) -> list[str]:
        return [c.strip().upper() for c in v]


class JobRoomSourceConfig(BaseModel):
    enabled: bool = True
    base_url: str = "https://www.job-room.ch"
    search_path: str = "/jobadservice/api/jobAdvertisements/_search"
    detail_path: str = "/jobadservice/api/jobAdvertisements/{id}"
    page_size: int = Field(default=500, ge=1, le=500)
    max_result_window: int = 10_000
    default_since_days: int = Field(default=1, ge=1)


class ChMediaHostConfig(BaseModel):
    enabled: bool = True
    base_url: str
    sitemaps: list[str] = Field(default_factory=list)


class ChMediaSourceConfig(BaseModel):
    enabled: bool = False
    hosts: dict[str, ChMediaHostConfig] = Field(default_factory=dict)

    def active_hosts(self) -> dict[str, ChMediaHostConfig]:
        return {k: v for k, v in self.hosts.items() if v.enabled}


class SourcesConfig(BaseModel):
    job_room: JobRoomSourceConfig = Field(default_factory=JobRoomSourceConfig)
    ch_media: ChMediaSourceConfig = Field(default_factory=ChMediaSourceConfig)


class QualityConfig(BaseModel):
    agency_penalty: float = Field(default=0.35, ge=0, le=1)
    max_per_company_in_digest: int = Field(default=3, ge=1)
    near_duplicate_threshold: float = Field(default=0.85, ge=0, le=1)


class Config(BaseModel):
    storage: StorageConfig = Field(default_factory=StorageConfig)
    politeness: PolitenessConfig = Field(default_factory=PolitenessConfig)
    region: RegionConfig = Field(default_factory=RegionConfig)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    quality: QualityConfig = Field(default_factory=QualityConfig)

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        path = path or DEFAULT_CONFIG_PATH
        if not path.exists():
            return cls()
        raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls.model_validate(raw)


# --------------------------------------------------------------------------
# Suchprofile (personenbezogen, gitignored)
# --------------------------------------------------------------------------


class LocationPreference(BaseModel):
    """Wunschort als Mittelpunkt plus Radius.

    ``lat``/``lon`` werden bevorzugt; ist nur ``postal_code`` gesetzt, löst der
    Geocoding-Schritt sie aus der PLZ-Referenztabelle auf.
    """

    label: str
    postal_code: str | None = None
    lat: float | None = None
    lon: float | None = None
    radius_km: float = Field(default=25.0, gt=0)


class TargetRole(BaseModel):
    """Eine Zielrolle beschreibt den WUNSCHZUSTAND, nicht den Ist-Zustand.

    Das ist der Grund, warum hier ein Freitext steht und nicht einfach der CV
    wiederverwendet wird: wer sich wegbewerben will, findet über den eigenen
    Lebenslauf genau das, was er schon macht.
    """

    name: str
    description: str


class SeniorityPreference(BaseModel):
    """Das eigene Niveau — damit unerreichbare Stellen abgewertet werden.

    Ohne diese Angaben rankt die Pipeline rein nach fachlicher Ähnlichkeit.
    Eine "Teamleiter:in Administration" ist inhaltlich fast deckungsgleich mit
    einer "Sachbearbeiterin Administration"; erreichbar ist ein Jahr nach dem
    EFZ aber nur die zweite.
    """

    #: Höchster eigener Abschluss: keine | efz | fachausweis | hf | fh | uni
    education: str = "efz"
    #: Berufsjahre nach der Ausbildung.
    years_experience: int = Field(default=1, ge=0)
    #: Wie viele Jahre über dem eigenen Stand noch akzeptabel sind.
    tolerance_years: int = Field(default=2, ge=0)
    #: Führungsstellen anzeigen?
    accept_leadership: bool = True
    #: Senior- und Spezialistenrollen anzeigen?
    accept_senior: bool = True


class ScoreWeights(BaseModel):
    semantic: float = 1.0
    workload: float = 0.5
    location: float = 0.8
    home_office: float = 0.3
    flextime: float = 0.3
    keywords: float = 0.5
    recency: float = 0.2
    agency_penalty: float = 0.35
    exclude_penalty: float = 1.0
    seniority_penalty: float = 0.8


class Profile(BaseModel):
    name: str
    display_name: str
    cv_text: str = ""
    target_roles: list[TargetRole] = Field(default_factory=list)

    locations: list[LocationPreference] = Field(default_factory=list)
    workload_min: int = Field(default=80, ge=0, le=100)
    workload_max: int = Field(default=100, ge=0, le=100)

    keywords_positive: list[str] = Field(default_factory=list)
    keywords_exclude: list[str] = Field(default_factory=list)

    wants_home_office: bool = False
    wants_flextime: bool = False
    include_agencies: bool = True

    seniority: SeniorityPreference = Field(default_factory=SeniorityPreference)
    weights: ScoreWeights = Field(default_factory=ScoreWeights)

    @field_validator("workload_max")
    @classmethod
    def _range_ok(cls, v: int, info: Any) -> int:
        lo = info.data.get("workload_min")
        if lo is not None and v < lo:
            raise ValueError("workload_max darf nicht kleiner als workload_min sein")
        return v

    @classmethod
    def load(cls, name: str, profiles_dir: Path | None = None) -> Profile:
        d = profiles_dir or PROFILES_DIR
        path = d / f"{name}.yaml"
        if not path.exists():
            available = sorted(p.stem for p in d.glob("*.yaml"))
            raise FileNotFoundError(
                f"Profil '{name}' nicht gefunden unter {path}. "
                f"Vorhanden: {', '.join(available) or '(keine)'}"
            )
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        raw.setdefault("name", name)
        return cls.model_validate(raw)

    @classmethod
    def list_available(cls, profiles_dir: Path | None = None) -> list[str]:
        d = profiles_dir or PROFILES_DIR
        return sorted(p.stem for p in d.glob("*.yaml"))


LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


def _resolve(p: Path) -> Path:
    return p if p.is_absolute() else REPO_ROOT / p
