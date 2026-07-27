"""Bewertungen speichern und auswerten.

Zweck ist nicht das Archivieren von Meinungen, sondern die **Kalibrierung der
Gewichte** in M6. Aktuell sind sie geraten; erst genug bewertete Treffer machen
aus dem Raten eine Rechnung.

Deshalb wird bei einer Ablehnung nach dem Grund gefragt. Ein blosses Nein sagt
nur, dass etwas nicht passte — der Grund verrät, **welches Gewicht** daneben
liegt: "zu weit weg" zieht am Ortsgewicht, "falscher Beruf" am semantischen
Anteil, "Vermittler" am Malus.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

#: Ablehnungsgründe. Bewusst kurz und auf das gemappt, was ein Gewicht hat.
REJECT_REASONS: dict[str, str] = {
    "ort": "zu weit weg",
    "beruf": "falscher Beruf / falsches Fachgebiet",
    "pensum": "Pensum passt nicht",
    "vermittler": "Personalvermittler",
    "senior": "Anforderungen zu hoch / zu senior",
    "junior": "zu wenig anspruchsvoll",
    "arbeitszeit": "Arbeitszeitmodell passt nicht",
    "firma": "Arbeitgeber kommt nicht in Frage",
    "anderes": "anderer Grund",
}

#: Welches Gewicht ein Grund infrage stellt. Grundlage der Kalibrierung.
#:
#: ``senior`` zeigte ursprünglich auf ``keywords`` — aus der Zeit, als es noch
#: gar kein Gewicht für das Anforderungsniveau gab. Ein höheres Keyword-Gewicht
#: drückt eine zu hoch gehängte Stelle aber nicht nach unten, es hebt sie eher
#: an: "Teamleiter:in Administration" enthält dieselben Positiv-Stichworte wie
#: die Sachbearbeitungsstelle daneben. Der Grund gehört auf
#: ``seniority_penalty``.
REASON_TO_WEIGHT: dict[str, str] = {
    "ort": "location",
    "beruf": "semantic",
    "pensum": "workload",
    "vermittler": "agency_penalty",
    "arbeitszeit": "flextime",
    "senior": "seniority_penalty",
    # Schwächste Zuordnung: für "zu wenig anspruchsvoll" gibt es kein eigenes
    # Gewicht. Positiv-Stichworte höher zu gewichten schiebt anspruchsvollere
    # Stellen nach oben, ist aber ein Umweg. Vorschläge daraus mit Vorsicht.
    "junior": "keywords",
}

RATING_LABELS = {1: "interessant", 0: "vielleicht", -1: "nein"}


@dataclass
class Rating:
    profile: str
    job_rowid: int
    rating: int
    reason: str | None = None
    note: str | None = None
    score_seen: float | None = None


def save(conn: sqlite3.Connection, r: Rating) -> None:
    conn.execute(
        """INSERT INTO feedback (profile, job_rowid, rating, reason, note, score_seen, created_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT (profile, job_rowid) DO UPDATE SET
               rating = excluded.rating,
               reason = excluded.reason,
               note = excluded.note,
               score_seen = excluded.score_seen,
               created_at = excluded.created_at""",
        (
            r.profile,
            r.job_rowid,
            r.rating,
            r.reason,
            r.note,
            r.score_seen,
            datetime.now(UTC).isoformat(timespec="seconds"),
        ),
    )


def pending(
    conn: sqlite3.Connection,
    profile: str,
    limit: int = 20,
    only_digest: bool = False,
) -> list[sqlite3.Row]:
    """Unbewertete Treffer, bestplatzierte zuerst.

    ``only_digest=True`` beschränkt auf das, was tatsächlich im Digest stand.
    Das war die ursprüngliche Voreinstellung und ist als Idee vertretbar —
    praktisch bremst sie aber: bei zwölf Treffern pro Digest bräuchte man
    allein fürs Sammeln der rund 40 Bewertungen, ab denen eine Kalibrierung
    Sinn ergibt, vier Tage.

    Standard ist deshalb der gesamte bewertete Bestand. Für die Kalibrierung
    ist ohnehin unerheblich, über welchen Kanal ein Treffer gesehen wurde —
    entscheidend ist, dass ein Mensch ihn beurteilt hat.
    """
    source = (
        """FROM digest_seen d
             JOIN jobs j   ON j.id = d.job_rowid
             JOIN scores s ON s.job_rowid = d.job_rowid AND s.profile = d.profile
        LEFT JOIN feedback f ON f.job_rowid = d.job_rowid AND f.profile = d.profile
            WHERE d.profile = ?"""
        if only_digest
        else """FROM scores s
             JOIN jobs j ON j.id = s.job_rowid
        LEFT JOIN feedback f ON f.job_rowid = s.job_rowid AND f.profile = s.profile
            WHERE s.profile = ?"""
    )
    rows: list[sqlite3.Row] = conn.execute(
        f"""SELECT j.id, j.title, j.company_name, j.city, j.company_is_agency,
                  j.description_md, j.source_url, j.apply_url,
                  j.workload_min, j.workload_max,
                  s.final_score, s.best_role, s.distance_km, s.rules_json
             {source}
              AND f.job_rowid IS NULL AND j.status = 'active'
              AND j.is_group_representative = 1
         ORDER BY s.final_score DESC
            LIMIT ?""",
        (profile, limit),
    ).fetchall()
    return rows


def stats(conn: sqlite3.Connection, profile: str) -> dict[str, Any]:
    total = conn.execute(
        "SELECT COUNT(*) n FROM feedback WHERE profile = ?", (profile,)
    ).fetchone()["n"]
    by_rating = {
        RATING_LABELS.get(r["rating"], str(r["rating"])): r["n"]
        for r in conn.execute(
            "SELECT rating, COUNT(*) n FROM feedback WHERE profile = ? GROUP BY rating",
            (profile,),
        )
    }
    by_reason = {
        REJECT_REASONS.get(r["reason"], r["reason"]): r["n"]
        for r in conn.execute(
            """SELECT reason, COUNT(*) n FROM feedback
                WHERE profile = ? AND reason IS NOT NULL
             GROUP BY reason ORDER BY n DESC""",
            (profile,),
        )
    }
    open_count = conn.execute(
        """SELECT COUNT(*) n FROM scores s
             JOIN jobs j ON j.id = s.job_rowid
        LEFT JOIN feedback f ON f.job_rowid = s.job_rowid AND f.profile = s.profile
            WHERE s.profile = ? AND f.job_rowid IS NULL
              AND j.status = 'active' AND j.is_group_representative = 1""",
        (profile,),
    ).fetchone()["n"]

    return {
        "bewertet": total,
        "offen": open_count,
        "nach_bewertung": by_rating,
        "ablehnungsgründe": by_reason,
        "reicht_für_kalibrierung": total >= MIN_RATINGS_FOR_CALIBRATION,
    }


#: Faustregel, ab wann eine Kalibrierung mehr ist als Rauschen. Darunter
#: bestimmen einzelne Ausreisser das Ergebnis.
MIN_RATINGS_FOR_CALIBRATION = 40


def rated_scores(conn: sqlite3.Connection, profile: str) -> list[tuple[int, float, str]]:
    """(Bewertung, Score, Ablehnungsgrund) — Rohmaterial für M6."""
    return [
        (int(r["rating"]), float(r["score_seen"] or 0.0), r["reason"] or "")
        for r in conn.execute(
            "SELECT rating, score_seen, reason FROM feedback WHERE profile = ?", (profile,)
        )
    ]


def separation(conn: sqlite3.Connection, profile: str) -> dict[str, float | None]:
    """Trennt der Score bereits zwischen gut und schlecht?

    Die einfachste sinnvolle Kennzahl vor der eigentlichen Kalibrierung: liegt
    der mittlere Score der interessanten Treffer über dem der abgelehnten?
    Wenn nicht, hilft kein Feintuning der Gewichte — dann stimmt etwas
    Grundsätzliches nicht.
    """
    rows = rated_scores(conn, profile)
    pos = [s for r, s, _ in rows if r == 1]
    neg = [s for r, s, _ in rows if r == -1]
    avg_pos = sum(pos) / len(pos) if pos else None
    avg_neg = sum(neg) / len(neg) if neg else None
    return {
        "schnitt_interessant": round(avg_pos, 4) if avg_pos is not None else None,
        "schnitt_nein": round(avg_neg, 4) if avg_neg is not None else None,
        "abstand": round(avg_pos - avg_neg, 4)
        if avg_pos is not None and avg_neg is not None
        else None,
    }
