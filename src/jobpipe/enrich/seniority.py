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
    "im idealfall",
    "und/oder",
    "wenn möglich",
    "bevorzugt",
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

#: Ausgeschriebene Zahlwörter.
#:
#: Gemessen nennen 356 Inserate die Berufsjahre als Ziffer und **79 als
#: ausgeschriebenes Wort** — knapp ein Fünftel. Darunter war der
#: höchstplatzierte Treffer des Profils B ("Mindestens zwei Jahre Erfahrung in
#: der HR Administration"), für den ``detect()`` deshalb gar keine
#: Anforderungen fand.
NUMBER_WORDS: dict[str, int] = {
    "einem": 1,
    "einen": 1,
    "eine": 1,
    "ein": 1,
    "zwei": 2,
    "drei": 3,
    "vier": 4,
    "fünf": 5,
    "sechs": 6,
    "sieben": 7,
    "acht": 8,
    "neun": 9,
    "zehn": 10,
}

#: Längere Wörter zuerst, sonst schluckt "ein" das "eine".
_NUMBER_WORD_ALT = "|".join(sorted(NUMBER_WORDS, key=len, reverse=True))

YEARS_RE = re.compile(
    r"(?:mindestens\s+|min\.?\s+|ca\.?\s+)?"
    rf"\b(\d{{1,2}}|{_NUMBER_WORD_ALT})\b"
    r"\s*(?:\+|\s*bis\s*\d+)?\s*"
    r"Jahre?\s+(?:Berufs)?[Ee]rfahrung",
    re.IGNORECASE,
)

#: Verlangte Weiterbildung ohne erkennbare Stufe.
#:
#: ``EDUCATION_PATTERNS`` kennt nur HF, FH, Uni und Fachausweis. Eine
#: "Weiterbildung als HR Sachbearbeiter:in" ist keine davon und war damit
#: unsichtbar — obwohl genau sie die Hürde für eine Berufseinsteigerin ist.
#: Gemessen betrifft das 349 Inserate.
FURTHER_EDUCATION_RE = re.compile(
    r"weiterbildung\s+(?:als|zum|zur|im\s+bereich)\s+[\w/:.-]+",
    re.IGNORECASE,
)

#: Hebt eine Weiterbildungsanforderung ganz auf: der Arbeitgeber bietet sie an
#: oder erwartet nur die Bereitschaft dazu.
#:
#: Aus den Stichproben: "Bereitschaft für Weiterbildung zum Wasserwart",
#: "Karriere mit Perspektive – Weiterbildung zum Automobil-Mechatroniker",
#: "Weiterbildung als Berufsbildner/in oder Bereitschaft, diese zu
#: absolvieren". Das sind Angebote, keine Hürden.
EDUCATION_OFFER_MARKERS = (
    "bereitschaft",
    "bieten wir",
    "wir bieten",
    "perspektive",
    "unterstütz",
    "fördern",
    "ermöglichen",
    "bei interesse",
    "berufsbegleitend",
)

#: Sprachen im Anforderungstext. Nur Fremdsprachen, die in Schweizer Inseraten
#: tatsächlich als Bedingung auftauchen — Deutsch wird nicht geprüft, weil die
#: Inserate selbst deutsch sind.
LANGUAGE_PATTERNS: dict[str, re.Pattern[str]] = {
    "fr": re.compile(r"franz[öo]sisch\w*|français|\bfranz\.\s*kenntnis", re.IGNORECASE),
    "it": re.compile(r"italienisch\w*|italiano", re.IGNORECASE),
    "en": re.compile(r"englisch\w*|english", re.IGNORECASE),
}

#: Formulierungen, die eine Anforderung zur Bedingung machen.
#:
#: Das Gegenstück zu :data:`SOFT_MARKERS`. Gemessen an ihren Ablehnungen war
#: "zwingend" der wiederkehrende Grund: das Inserat sagt ausdrücklich, dass
#: ohne diese Sprache nichts geht. Solche Stellen gehören nicht nach oben.
HARD_MARKERS = (
    "zwingend",
    "vorausgesetzt",
    "setzen wir voraus",
    "voraussetzung",
    "unabdingbar",
    "zwingende bedingung",
    "muss",
    "erforderlich",
    "bedingung",
    "verhandlungssicher",
    "stilsicher",
    "muttersprache",
)

#: Trennt Teilsätze. Der Geltungsbereich eines "von Vorteil" endet am Komma,
#: nicht erst am Satzende.
#:
#: Das Komma ist die entscheidende Grenze, und sie hat zwei Anläufe gebraucht.
#: Ohne sie las die Regel
#:
#:     "zwingend sehr gute Französisch- und Italienischkenntnisse,
#:      weitere Sprachen sind von Vorteil"
#:
#: als weich — das "von Vorteil" gehört aber zu "weitere Sprachen". Umgekehrt
#: darf ein "zwingend" nicht auf den Nachbarsatz abfärben:
#:
#:     "Englisch zwingend erforderlich, Französisch von Vorteil"
#:
#: Beide Fälle stammen aus echten Inseraten und gehen nur mit Komma-Grenze
#: gleichzeitig auf.
SEGMENT_RE = re.compile(r"[\n;,]|(?<=[a-zäöüß])\.\s|\s[-*•]\s")

#: Tags, die im gerenderten Text eine neue Zeile bedeuten.
_BLOCK_TAG_RE = re.compile(r"</?(?:br|li|p|div|tr|ul|ol|h[1-6])\b[^>]*>", re.IGNORECASE)
_ANY_TAG_RE = re.compile(r"<[^>]+>")


def as_lines(text: str) -> str:
    """HTML entfernen, aber die Zeilenstruktur behalten.

    :func:`~jobpipe.parse.schema.strip_html` presst jeden Umbruch zu einem
    Leerzeichen. Für die Bildungs- und Jahreserkennung ist das richtig, für
    die Sprachen nicht: dort ist die Zeilengrenze die Grenze, an der ein
    "von Vorteil" aufhört zu gelten. Ohne sie hing der ganze Anforderungsteil
    in einem einzigen Segment.
    """
    text = _BLOCK_TAG_RE.sub("\n", text)
    text = _ANY_TAG_RE.sub(" ", text)
    return re.sub(r"[^\S\n]+", " ", text)

#: Wie sicher eine Sprache verlangt wird.
LANG_HARD = 1.0  # "zwingend", "Voraussetzung"
LANG_PROBABLE = 0.5  # Arbeitssprache ohne Einschränkung
LANG_SOFT = 0.0  # "von Vorteil", "idealerweise"


def _segment_containing(text: str, pos: int) -> str:
    """Der Aufzählungspunkt oder Satz, in dem eine Fundstelle steht."""
    start = 0
    end = len(text)
    for m in SEGMENT_RE.finditer(text):
        if m.end() <= pos:
            start = m.end()
        elif m.start() > pos:
            end = m.start()
            break
    return text[start:end]


def required_languages(text: str) -> dict[str, float]:
    """Welche Fremdsprachen das Inserat nennt und wie verbindlich.

    Drei Stufen statt zwei, weil die Wirklichkeit drei kennt. Gemessen an 181
    Inseraten mit "Französisch":

    * **hart** — "Du verfügst zwingend über sehr gute Französischkenntnisse".
      Nicht verhandelbar.
    * **wahrscheinlich** — die Sprache steht ohne Einschränkung in den
      Aufgaben: "Kundenbetreuung in den Sprachen Deutsch, Französisch und
      Englisch". Kein Markerwort, aber wer kein Französisch spricht, kann die
      Aufgabe nicht erfüllen. Das war die grosse Lücke der ersten Fassung: 164
      von 181 Fällen trugen gar kein Markerwort.
    * **weich** — "Französisch von Vorteil". Keine Absage.

    Der Geltungsbereich eines Markers endet am Satz- oder Zeilenende. Ohne
    diese Begrenzung machte ein "von Vorteil" am Ende einer Aufzählung die
    ganze Aufzählung weich, und ein "zwingend" für Englisch färbte auf
    Französisch ab.
    """
    found: dict[str, float] = {}
    for code, pattern in LANGUAGE_PATTERNS.items():
        match = pattern.search(text)
        if not match:
            continue
        segment = _segment_containing(text, match.start()).lower()
        if any(m in segment for m in SOFT_MARKERS):
            found[code] = LANG_SOFT
        elif any(m in segment for m in HARD_MARKERS):
            found[code] = LANG_HARD
        else:
            found[code] = LANG_PROBABLE
    return found


@dataclass
class Requirements:
    """Was eine Stelle verlangt."""

    is_leadership: bool = False
    is_senior: bool = False
    is_entry_friendly: bool = False
    education: str = "keine"
    education_is_soft: bool = False
    #: Verlangte Weiterbildung ohne erkennbare Stufe, als Wortlaut.
    further_education: str | None = None
    further_education_is_soft: bool = False
    years: int | None = None
    #: Sprachkürzel -> Verbindlichkeit (LANG_HARD | LANG_PROBABLE | LANG_SOFT).
    languages: dict[str, float] = field(default_factory=dict)
    signals: list[str] = field(default_factory=list)

    @property
    def education_rank(self) -> int:
        return EDUCATION_RANK.get(self.education, 0)


def _years_to_int(token: str) -> int | None:
    """Ziffer oder ausgeschriebenes Zahlwort in eine Zahl."""
    if token.isdigit():
        return int(token)
    return NUMBER_WORDS.get(token.lower())


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

    # Weiterbildung ohne Stufe. Nur prüfen, wenn keine höhere Stufe gefunden
    # wurde — "Weiterbildung zum Techniker HF" ist bereits über HF erfasst.
    if req.education == "keine":
        match_wb = FURTHER_EDUCATION_RE.search(as_lines(description or ""))
        if match_wb:
            segment = _segment_containing(
                as_lines(description or ""), match_wb.start()
            ).lower()
            if not any(m in segment for m in EDUCATION_OFFER_MARKERS):
                req.further_education = match_wb.group(0)
                req.further_education_is_soft = any(m in segment for m in SOFT_MARKERS)
                req.signals.append(
                    f"Weiterbildung {'erwünscht' if req.further_education_is_soft else 'gefordert'}"
                )

    match_years = YEARS_RE.search(text)
    if match_years:
        req.years = _years_to_int(match_years.group(1))
        if req.years is not None:
            req.signals.append(f"{req.years} Jahre Erfahrung gefordert")

    # Bewusst nicht ``text``: die Sprachregel braucht die Zeilenstruktur.
    req.languages = required_languages(as_lines(description or ""))
    for lang_code, lang_level in req.languages.items():
        if lang_level >= LANG_HARD:
            req.signals.append(f"{lang_code.upper()} zwingend gefordert")
        elif lang_level >= LANG_PROBABLE:
            req.signals.append(f"{lang_code.upper()} als Arbeitssprache")

    return req


def penalty(
    req: Requirements,
    *,
    own_education: str = "efz",
    own_years: int = 1,
    accept_leadership: bool = True,
    accept_senior: bool = True,
    tolerance_years: int = 2,
    own_languages: tuple[str, ...] = ("de",),
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

    # Weiterbildung ohne Stufe. Sie liegt zwischen EFZ und Fachausweis, wiegt
    # also wie ein Schritt in der Bildungsleiter. Wer selbst schon höher steht,
    # ist davon nicht betroffen.
    if req.further_education and EDUCATION_RANK.get(own_education, 1) <= EDUCATION_RANK["efz"]:
        total += 0.15 if req.further_education_is_soft else 0.35
        parts.append(
            f"Weiterbildung {'erwünscht' if req.further_education_is_soft else 'gefordert'}"
        )

    if req.years is not None and req.years > own_years + tolerance_years:
        total += min(0.4, 0.1 * (req.years - own_years - tolerance_years))
        parts.append(f"{req.years} Jahre Erfahrung")

    # Eine fehlende Fremdsprache wiegt schwerer als alles andere hier: sie
    # lässt sich nicht durch ein gutes Anschreiben ausgleichen. Abgestuft nach
    # Verbindlichkeit — "von Vorteil" (LANG_SOFT) kostet nichts.
    fehlend = {
        code: level
        for code, level in req.languages.items()
        if level > LANG_SOFT and code not in own_languages
    }
    if fehlend:
        total += max(fehlend.values())
        hart = sorted(c.upper() for c, lv in fehlend.items() if lv >= LANG_HARD)
        weich = sorted(c.upper() for c, lv in fehlend.items() if lv < LANG_HARD)
        if hart:
            parts.append(f"{'/'.join(hart)} zwingend gefordert")
        if weich:
            parts.append(f"{'/'.join(weich)} als Arbeitssprache")

    # Ausdrücklich für Einsteigende geöffnete Stellen: Hürde halbiert.
    if req.is_entry_friendly and total > 0:
        total *= 0.5
        parts.append("Einstieg aber willkommen")

    return min(1.0, total), (", ".join(parts) if parts else "")
