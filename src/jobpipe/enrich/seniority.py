"""Anforderungsniveau einer Stelle.

Beantwortet die Frage, die das übrige Scoring nicht stellt: **ist diese Stelle
überhaupt erreichbar?** Fachliche Ähnlichkeit sagt nichts über die Hürde. Eine
"Teamleiter:in Administration" ist inhaltlich fast deckungsgleich mit einer
"Sachbearbeiterin Administration" — für jemanden ein Jahr nach dem EFZ ist sie
trotzdem keine Option.

Was verlässlich ist und was nicht
---------------------------------
Gemessen an 50 echten Treffern:

* **Titel-Marker sind verlässlich.** "Teamleiter:in", "Teamleader",
  "Mandatsleiter/in", "Fachspezialist:in", "Payroll Spezialist" — genau die
  Fälle, die stören, tragen es im Titel.
* **Fliesstext-Treffer auf "Führung" sind es nicht.** 21 von 50 Inseraten
  enthielten das Wort irgendwo, fast alle harmlos ("in einem Führungsteam",
  "unter der Leitung von"). Deshalb zählt für Führung nur der Titel.
* **Explizite Berufsjahre sind selten.** Nur 2 von 50 nannten eine Zahl. Als
  alleiniges Kriterium unbrauchbar, als Zusatzsignal brauchbar.
* **Bildungsanforderungen stehen im Anforderungsteil** und lassen sich in hart
  ("Abgeschlossene höhere Ausbildung (HF/FH/Uni)") und weich ("idealerweise mit
  einem HR-Fachausweis") trennen. Die Unterscheidung zählt: weich formuliert
  ist keine Absage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from jobpipe.parse.schema import strip_html

#: Bildungsstufen als Rangfolge. Höher heisst höhere Hürde.
EDUCATION_RANK: dict[str, int] = {
    "keine": 0,
    "efz": 1,
    "fachausweis": 2,
    "hf": 3,
    "fh": 4,
    "uni": 5,
}

#: Führungsverantwortung — nur im Titel gewertet.
#:
#: **Keine führende Wortgrenze bei zusammensetzbaren Wörtern.** Deutsch bildet
#: Komposita, und mit ``\bleiter`` fielen "Abteilungsleiter" und
#: "Projektleiter" durch. Derselbe Fehler wie zuvor bei "Kantonsspital".
LEADERSHIP_TITLE_RE = re.compile(
    r"(leiter(?:in)?\b|leitung\b|leiten\w*|führung\w*|"
    r"team\s?lead\w*|head\s+of|\bchef(?:in)?\b|geschäftsführ\w*)",
    re.IGNORECASE,
)

#: Erfahrungsstufe — ebenfalls nur im Titel, ebenfalls kompositafähig.
SENIOR_TITLE_RE = re.compile(
    r"(\bsenior\b|spezialist\w*|expert(?:e|in|en)?\b|manager(?:in)?\b|"
    r"verantwortliche[rn]?\b|\bprincipal\b|\blead\b|koordinator\w*)",
    re.IGNORECASE,
)

#: Hebt die Senior-Einstufung auf: ausdrücklich für den Einstieg gedacht.
JUNIOR_TITLE_RE = re.compile(r"\b(junior|einsteiger|trainee|nachwuchs|assistent)", re.IGNORECASE)

#: Bildungsabschlüsse im Anforderungstext.
EDUCATION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("uni", re.compile(r"\b(universit|master|uni/fh|bachelor|studium|hochschulabschluss)\b", re.I)),
    ("fh", re.compile(r"\b(fachhochschul\w*|\bFH\b|\bBSc\b|\bBA\b)\b", re.I)),
    ("hf", re.compile(r"\b(höhere\s+fachschule|\bHF\b|dipl\.?\s*techniker)\b", re.I)),
    ("fachausweis", re.compile(r"\b(fachausweis|eidg\.?\s*dipl|\bBP\b|\bHFP\b)\b", re.I)),
]

#: Weiche Formulierungen. "HF von Vorteil" ist keine Absage.
SOFT_MARKERS = (
    "von vorteil",
    "idealerweise",
    "wünschenswert",
    "erwünscht",
    "ein plus",
    "nice to have",
    "vorteilhaft",
    "oder vergleichbar",
    "oder gleichwertig",
    "äquivalent",
)

#: Ausdrückliche Einladung an Berufseinsteigende — hebt Hürden teilweise auf.
ENTRY_WELCOME_RE = re.compile(
    r"\b(berufseinsteig\w*|einsteiger\w*|lehrabgäng\w*|erste\s+berufserfahrung|"
    r"auch\s+ohne\s+erfahrung|frisch\s+ab\s+lehre)",
    re.IGNORECASE,
)

YEARS_RE = re.compile(
    r"(?:mindestens\s+|min\.?\s+|ca\.?\s+)?(\d{1,2})\s*(?:\+|\s*bis\s*\d+)?\s*"
    r"Jahre?\s+(?:Berufs)?[Ee]rfahrung",
    re.IGNORECASE,
)


@dataclass
class Requirements:
    """Was eine Stelle verlangt."""

    is_leadership: bool = False
    is_senior: bool = False
    is_entry_friendly: bool = False
    education: str = "keine"
    education_is_soft: bool = False
    years: int | None = None
    signals: list[str] = field(default_factory=list)

    @property
    def education_rank(self) -> int:
        return EDUCATION_RANK.get(self.education, 0)


def detect(title: str, description: str) -> Requirements:
    """Liest das Anforderungsniveau aus Titel und Text."""
    req = Requirements()
    text = strip_html(description or "")
    lowered = text.lower()

    if LEADERSHIP_TITLE_RE.search(title):
        req.is_leadership = True
        req.signals.append("Führungsfunktion im Titel")

    if SENIOR_TITLE_RE.search(title) and not JUNIOR_TITLE_RE.search(title):
        req.is_senior = True
        req.signals.append("Spezialisten- oder Senior-Rolle im Titel")

    if ENTRY_WELCOME_RE.search(text):
        req.is_entry_friendly = True
        req.signals.append("Berufseinstieg ausdrücklich willkommen")

    # Höchste genannte Bildungsstufe gewinnt.
    for level, pattern in EDUCATION_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        req.education = level
        window = lowered[max(0, match.start() - 120) : match.end() + 120]
        req.education_is_soft = any(marker in window for marker in SOFT_MARKERS)
        req.signals.append(
            f"{level.upper()} {'erwünscht' if req.education_is_soft else 'gefordert'}"
        )
        break

    match_years = YEARS_RE.search(text)
    if match_years:
        try:
            req.years = int(match_years.group(1))
            req.signals.append(f"{req.years} Jahre Erfahrung gefordert")
        except ValueError:
            pass

    return req


def penalty(
    req: Requirements,
    *,
    own_education: str = "efz",
    own_years: int = 1,
    accept_leadership: bool = True,
    accept_senior: bool = True,
    tolerance_years: int = 2,
) -> tuple[float, str]:
    """Wie weit liegt die Stelle über dem eigenen Niveau?

    Rückgabe ist eine **Strafe** zwischen 0 und 1 plus die Begründung. Bewusst
    keine harte Ausschlussregel: eine zu hoch gehängte Stelle ist nicht
    unmöglich, nur unwahrscheinlicher — und wer sich bewerben will, soll das
    entscheiden, nicht der Filter.
    """
    parts: list[str] = []
    total = 0.0

    # Wer Führung ausdrücklich ablehnt, meint das auch. Volle Strafe statt
    # einer Abwertung, die den Treffer nur ein paar Plätze nach unten schiebt.
    if req.is_leadership and not accept_leadership:
        total += 1.0
        parts.append("Führungsfunktion")

    if req.is_senior and not accept_senior:
        total += 0.7
        parts.append("Senior-/Spezialistenrolle")

    gap = req.education_rank - EDUCATION_RANK.get(own_education, 1)
    if gap > 0:
        # Weich formuliert wiegt nur ein Drittel: "HF von Vorteil" schliesst
        # niemanden aus.
        weight = 0.15 if req.education_is_soft else 0.35
        step = min(1.0, gap * weight)
        total += step
        parts.append(
            f"{req.education.upper()} {'erwünscht' if req.education_is_soft else 'gefordert'}"
        )

    if req.years is not None and req.years > own_years + tolerance_years:
        total += min(0.4, 0.1 * (req.years - own_years - tolerance_years))
        parts.append(f"{req.years} Jahre Erfahrung")

    # Ausdrücklich für Einsteigende geöffnete Stellen: Hürde halbiert.
    if req.is_entry_friendly and total > 0:
        total *= 0.5
        parts.append("Einstieg aber willkommen")

    return min(1.0, total), (", ".join(parts) if parts else "")
