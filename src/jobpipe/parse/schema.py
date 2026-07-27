"""Das gemeinsame Zielschema.

Portal-unabhängig. Die Parser bauen ``JobPosting`` aus einer **expliziten
Feldliste** — was hier nicht steht, kann gar nicht erst ankommen. Das ist die
Allowlist aus PLAN.md, ausgedrückt als Typ statt als Filter.

Bewusst nicht enthalten: jede Form von Kontaktperson. Erhalten bleibt der
Bewerbungsweg als URL beziehungsweise als Kennzeichen, dass es ihn gibt.
"""

from __future__ import annotations

import hashlib
import html
import re
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, ValidationInfo, field_validator

Portal = Literal["job_room", "myjob", "ostjob", "zentraljob"]
JobStatus = Literal["active", "expired"]

_WS_RE = re.compile(r"\s+")
_HTML_TAG_RE = re.compile(r"<[^>]+>")

#: Deko im Stellentitel, die für Vergleich und Dedup nur stört.
_TITLE_NOISE_RE = re.compile(
    r"""
      \(\s*[mwdfhx]\s*(?:/\s*[mwdfhx]\s*)+\)   # (m/w/d), (w/m/d), (m/f/d)
    | \b[mwdfhx](?:\s*/\s*[mwdfhx])+\b          # m/w/d ohne Klammern
    | \d{1,3}\s*[–\-—]\s*\d{1,3}\s*%            # 80-100%
    | \b\d{1,3}\s*%                             # 100%
    | \(\s*\)                                   # leere Klammern
    """,
    re.IGNORECASE | re.VERBOSE,
)

#: Rechtsformen, die beim Firmenvergleich ignoriert werden.
_LEGAL_FORM_RE = re.compile(
    r"\b(ag|sa|gmbh|sàrl|sarl|srl|llc|ltd|inc|co|kg|ohg|holding|group|gruppe|"
    r"schweiz|switzerland|suisse)\b\.?",
    re.IGNORECASE,
)


#: Portale liefern Beschreibungen als Markdown mit maskierten Sonderzeichen
#: ("80\-100%", "Fresh Food \& Beverage"). In der Anzeige sind die Backslashes
#: nur Störung.
_MD_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|>~&])")


def unescape_markdown(text: str) -> str:
    """Entfernt Markdown-Maskierungen. Eine Definition für die ganze Pipeline."""
    return _MD_ESCAPE_RE.sub(r"\1", text)


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    return _WS_RE.sub(" ", _HTML_TAG_RE.sub(" ", text)).strip()


#: Gendermarker in Schweizer Stellentiteln: Logistiker*in, Logistiker/in,
#: Logistiker:in, Logistiker/-in, Logistiker_innen …
#:
#: Nur die Form MIT Trennzeichen wird abgeschnitten. Ein blosses "in" am
#: Wortende bleibt stehen — sonst würde aus "Medizin" ein "Mediz".
_GENDER_SUFFIX_RE = re.compile(r"(\w)[*/:_·]-?in(?:nen)?\b", re.IGNORECASE)

#: Werbefloskeln, die Massenausschreiber an den Titel hängen.
_TITLE_TAIL_RE = re.compile(
    r"""
      \s+(?:in|für|fuer|im\s+raum|region|raum)\s+[^,;–\-]{2,40}?
      \s+(?:gesucht|zu\s+besetzen)\s*$      # "… in Erlenbach gesucht"
    | \s+(?:gesucht|zu\s+besetzen)\s*$      # "… gesucht"
    # " – Spezialisiere dich auf …". Leerzeichen um den Strich sind Pflicht,
    # sonst zerlegt die Regel Pensumangaben wie "3-4 Schicht".
    | \s+[–—]\s+[^–—]{5,90}$
    | \s+-\s+[^-]{5,90}$
    """,
    re.IGNORECASE | re.VERBOSE,
)


def normalize_title(title: str, city: str | None = None) -> str:
    """Titel auf die vergleichbare Kernform bringen.

    ``Logistiker 100% (m/w/d)`` und ``Logistiker*in 80-100%`` werden beide zu
    ``logistiker``.

    Wichtiger Sonderfall: Massenausschreiber schreiben den Ort in den Titel
    (``Experte Anästhesiepflege NDS HF (80-100%) in Erlenbach gesucht``).
    Der Ort ist ein eigenes Feld und hat im Vergleichsschlüssel nichts
    verloren — sonst gilt dieselbe Stelle in 40 Gemeinden als 40 Stellen.
    """
    # HTML-Entities zuerst auflösen. Gemessen: "Teamleader ICT Finance &amp; HR
    # Applications" und "… Finance & HR Applications" galten als zwei Stellen,
    # obwohl es dieselbe war — der einzige Unterschied war die Kodierung.
    out = html.unescape(title)
    out = _GENDER_SUFFIX_RE.sub(r"\1", out)
    out = _TITLE_NOISE_RE.sub(" ", out)
    out = out.replace("*", "").replace("(in)", "")
    # Mehrfach anwenden: die Muster überlappen sich ("… in X gesucht – Y").
    for _ in range(3):
        stripped = _TITLE_TAIL_RE.sub("", out)
        if stripped == out:
            break
        out = stripped
    if city:
        out = re.sub(rf"\b{re.escape(city)}\b", " ", out, flags=re.IGNORECASE)
    return _WS_RE.sub(" ", out).strip(" -–—,;").lower()


def normalize_company(name: str) -> str:
    """Firmenname ohne Rechtsform und Interpunktion."""
    out = _LEGAL_FORM_RE.sub(" ", name)
    out = re.sub(r"[^\w\s]", " ", out, flags=re.UNICODE)
    return _WS_RE.sub(" ", out).strip().lower()


class JobPosting(BaseModel):
    """Ein normalisiertes Stelleninserat."""

    # -- Identität ---------------------------------------------------------
    portal: Portal
    source_id: str
    source_url: str
    cluster_id: str | None = None

    # -- Kern --------------------------------------------------------------
    title: str
    company_name: str
    company_is_agency: bool = False
    #: Welches Signal die Vermittler-Einstufung ausgelöst hat. Leer, wenn
    #: die Firma nicht als Vermittler gilt.
    agency_reason: str = ""
    description_md: str = ""
    description_truncated: bool = False

    # -- Ort ---------------------------------------------------------------
    city: str | None = None
    postal_code: str | None = None
    canton: str | None = None
    lat: float | None = None
    lon: float | None = None

    # -- Konditionen -------------------------------------------------------
    workload_min: int | None = Field(default=None, ge=0, le=100)
    workload_max: int | None = Field(default=None, ge=0, le=100)
    is_permanent: bool | None = None
    start_date: date | None = None
    home_office: bool | None = None
    # Kein Portal liefert Lohn. Felder existieren für den späteren
    # Adzuna-Benchmark, bleiben bis dahin leer.
    salary_min: int | None = None
    salary_max: int | None = None

    # -- Zeit --------------------------------------------------------------
    posted_at: datetime | None = None
    expires_at: datetime | None = None
    status: JobStatus = "active"

    # -- Bewerbung (ohne Personendaten) ------------------------------------
    apply_url: str | None = None
    apply_via_email: bool = False
    apply_via_phone: bool = False

    # -- Klassifikation ----------------------------------------------------
    occupation_codes: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)

    # -- Dedup -------------------------------------------------------------
    #: Gruppe mehrfach ausgeschriebener Stellen derselben Firma (48 Gemeinden,
    #: eine Stelle). Alle Zeilen bleiben erhalten, damit der Radiusfilter
    #: greift; der Digest zeigt pro Gruppe nur den passendsten Treffer.
    group_id: str | None = None
    group_size: int = 1
    is_group_representative: bool = True

    # -- Meta --------------------------------------------------------------
    raw_path: str | None = None

    @field_validator("title", "company_name")
    @classmethod
    def _tidy(cls, v: str) -> str:
        return _WS_RE.sub(" ", v).strip()

    @field_validator("workload_max")
    @classmethod
    def _workload_order(cls, v: int | None, info: ValidationInfo) -> int | None:
        """Vertauschte Angaben glätten statt den Datensatz zu verwerfen."""
        lo = info.data.get("workload_min")
        if v is not None and isinstance(lo, int) and v < lo:
            return lo
        return v

    # -- abgeleitet --------------------------------------------------------

    @property
    def title_key(self) -> str:
        return normalize_title(self.title, self.city)

    @property
    def company_key(self) -> str:
        return normalize_company(self.company_name)

    @property
    def content_hash(self) -> str:
        """Hash über den bereinigten Beschreibungstext.

        Erkennt identische Texte, die Vermittler für viele Ortschaften
        duplizieren.
        """
        basis = f"{self.title_key}|{strip_html(self.description_md).lower()}"
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]

    @property
    def dedup_block(self) -> str:
        """Blocking-Schlüssel: nur innerhalb desselben Blocks wird verglichen."""
        return self.postal_code or (self.city or "").lower() or "?"

    def workload_overlaps(self, wanted_min: int, wanted_max: int) -> bool:
        lo = self.workload_min if self.workload_min is not None else 0
        hi = self.workload_max if self.workload_max is not None else 100
        return lo <= wanted_max and hi >= wanted_min
