"""Sprache eines Inseratstexts erkennen.

Warum das nötig ist: **das Etikett lügt.** Ein STIHL-Inserat für Wil SG trägt
``jobDescriptions[].languageIsoCode = "de"``, während der Text mit «POURQUOI
STIHL. En tant qu'entreprise familiale innovante…» beginnt. job-room deckt die
ganze Schweiz ab, und jobup speist französische Inserate ein — gemessen sind
**2.5 % des Gesamtbestands** französisch, im kaufmännischen Pool von Profil B
aber **7.1 %**, weil gerade diese Berufsgruppen aus der Romandie kommen.

Bewusst kein ``langdetect`` oder ``fasttext``: bei mehreren hundert Zeichen
Inseratstext genügt das Zählen von Funktionswörtern. Das ist deterministisch,
ohne Modelldatei, in Millisekunden über 13'000 Inserate und — der eigentliche
Punkt — im Zweifelsfall nachvollziehbar falsch statt unerklärlich falsch.

Im Zweifel wird ``None`` zurückgegeben, und ``None`` heisst **behalten**. Ein
Inserat wegen einer unsicheren Spracherkennung zu verwerfen wäre der teurere
Fehler.
"""

from __future__ import annotations

import re
from collections import Counter

#: Funktionswörter je Sprache. Bewusst nur solche, die in der jeweils anderen
#: Sprache nicht oder kaum vorkommen — "in" etwa steht in Deutsch und
#: Italienisch und taugt deshalb nicht.
STOPWORDS: dict[str, frozenset[str]] = {
    "de": frozenset(
        ["und", "der", "die", "das", "den", "dem", "des", "mit", "für", "von", "bei", "sie", "wir", "uns", "eine", "einen", "einem", "ist", "sind", "wird", "werden", "dich", "dein", "deine", "ihre", "ihren", "nicht", "auch", "oder", "aber", "sowie", "durch", "über", "unter", "zwischen", "nach", "vor", "beim", "zum", "zur", "als", "wie", "sich", "haben", "hat"]
    ),
    "fr": frozenset(
        ["et", "le", "la", "les", "des", "du", "une", "un", "pour", "vous", "nous", "dans", "est", "sont", "votre", "notre", "avec", "qui", "que", "sur", "par", "au", "aux", "ce", "cette", "leur", "plus", "sera", "être", "avoir", "vos", "nos", "chez", "ainsi", "dont", "afin", "lors"]
    ),
    "it": frozenset(
        ["il", "lo", "la", "gli", "le", "dei", "delle", "del", "della", "per", "con", "una", "che", "nel", "nella", "sono", "anche", "come", "alla", "alle", "dal", "dalla", "suo", "sua", "loro", "più", "essere", "questo"]
    ),
    "en": frozenset(
        ["the", "and", "for", "you", "our", "with", "are", "your", "this", "that", "will", "have", "from", "their", "about", "which", "they", "been", "would", "there", "these", "those", "into"]
    ),
}

#: Wie viele Treffer die häufigste Sprache mindestens braucht.
#:
#: Darunter ist der Text zu kurz oder zu stichwortartig für eine Aussage —
#: gemessen 2.8 % der Inserate.
MIN_HITS = 5

#: Um wie viel die beste Sprache die zweitbeste schlagen muss.
#:
#: Deutsche Inserate zitieren oft englische Begriffe ("Home Office", "Team
#: Lead"), französische deutsche Ortsnamen. Ein knapper Vorsprung ist kein
#: Befund.
MIN_RATIO = 1.5

_WORD_RE = re.compile(r"[a-zà-öø-ÿ]+", re.IGNORECASE)

#: Wie viele Zeichen geprüft werden. Der Anfang eines Inserats ist
#: aussagekräftig genug, und es hält die Auswertung schnell.
SAMPLE_CHARS = 1500


def detect_language(text: str | None) -> str | None:
    """Sprachkürzel des Texts, oder ``None`` wenn unklar.

    ``None`` ist ein gültiges Ergebnis und bedeutet "keine Aussage" — nicht
    "keine der bekannten Sprachen".
    """
    if not text:
        return None
    woerter = Counter(w.lower() for w in _WORD_RE.findall(text[:SAMPLE_CHARS]))
    if not woerter:
        return None

    treffer = {
        code: sum(woerter[w] for w in wortliste) for code, wortliste in STOPWORDS.items()
    }
    rang = sorted(treffer.items(), key=lambda kv: kv[1], reverse=True)
    beste, bester_wert = rang[0]
    zweiter_wert = rang[1][1] if len(rang) > 1 else 0

    if bester_wert < MIN_HITS:
        return None
    if zweiter_wert and bester_wert < zweiter_wert * MIN_RATIO:
        return None
    return beste


def matches_language(detected: str | None, wanted: list[str] | tuple[str, ...]) -> bool:
    """Passt die erkannte Sprache zum Profil?

    ``wanted`` leer heisst: keine Einschränkung. Eine **unerkannte** Sprache
    passt immer — im Zweifel behalten.
    """
    if not wanted:
        return True
    if detected is None:
        return True
    return detected in wanted
