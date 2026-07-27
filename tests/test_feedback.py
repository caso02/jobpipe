"""Bewertungen speichern, abfragen und auswerten."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from jobpipe.config import Config, LocationPreference, Profile, TargetRole
from jobpipe.output import digest as dg
from jobpipe.parse.schema import JobPosting
from jobpipe.store import db
from jobpipe.store import feedback as fb
from jobpipe.store import jobs as jobs_store


def make_job(**kw: Any) -> JobPosting:
    base: dict[str, Any] = {
        "portal": "job_room",
        "source_id": "x",
        "source_url": "https://example.ch/1",
        "title": "Sachbearbeiter/in Innendienst",
        "company_name": "Muster AG",
        "lat": 47.556,
        "lon": 8.898,
        "city": "Frauenfeld",
        "workload_min": 80,
        "workload_max": 100,
        "posted_at": datetime.now(UTC),
    }
    return JobPosting(**(base | kw))


@pytest.fixture
def prepared(tmp_path: Path) -> tuple[Any, Profile]:
    """DB mit gescorten Inseraten, die bereits im Digest standen."""
    from jobpipe.enrich.score import run as score_run

    conn = db.init_db(tmp_path / "t.db")
    jobs = [
        make_job(
            source_id=str(i), title=f"Sachbearbeiter/in Bereich {i}", company_name=f"Firma {i}"
        )
        for i in range(6)
    ]
    jobs_store.upsert_many(conn, jobs)
    profile = Profile(
        name="test",
        display_name="Test",
        locations=[LocationPreference(label="Thundorf", lat=47.5525, lon=8.9317, radius_km=25)],
        target_roles=[TargetRole(name="Sachbearbeitung", description="Innendienst.")],
    )
    score_run(conn, profile, prefer_local=True)
    dg.build(conn, profile, Config(), top_n=6)
    return conn, profile


class TestPending:
    def test_all_scored_jobs_are_offered_by_default(self, tmp_path: Path) -> None:
        """Bewertbar ist der ganze bewertete Bestand, nicht nur der Digest.

        Die Beschränkung auf digest-gezeigte Treffer war die erste Fassung und
        als Idee vertretbar. Praktisch bremst sie: bei zwölf Treffern pro
        Digest bräuchte man allein fürs Sammeln der rund 40 Bewertungen, ab
        denen eine Kalibrierung Sinn ergibt, vier Tage. Für die Kalibrierung
        ist unerheblich, über welchen Kanal ein Treffer gesehen wurde.
        """
        from jobpipe.enrich.score import run as score_run

        conn = db.init_db(tmp_path / "t.db")
        jobs_store.upsert_many(conn, [make_job(source_id=str(i)) for i in range(5)])
        profile = Profile(
            name="test",
            display_name="Test",
            locations=[LocationPreference(label="T", lat=47.55, lon=8.93, radius_km=25)],
            target_roles=[TargetRole(name="R", description="Sachbearbeitung.")],
        )
        score_run(conn, profile, prefer_local=True)
        assert len(fb.pending(conn, "test")) == 5

    def test_digest_only_mode_still_available(self, tmp_path: Path) -> None:
        """``only_digest=True`` beschränkt weiterhin auf Gezeigtes."""
        from jobpipe.enrich.score import run as score_run

        conn = db.init_db(tmp_path / "t.db")
        jobs_store.upsert_many(conn, [make_job(source_id=str(i)) for i in range(5)])
        profile = Profile(
            name="test",
            display_name="Test",
            locations=[LocationPreference(label="T", lat=47.55, lon=8.93, radius_km=25)],
            target_roles=[TargetRole(name="R", description="Sachbearbeitung.")],
        )
        score_run(conn, profile, prefer_local=True)
        assert fb.pending(conn, "test", only_digest=True) == []

        dg.build(conn, profile, Config(), top_n=2)
        assert len(fb.pending(conn, "test", only_digest=True)) == 2

    def test_rated_entries_disappear(self, prepared: tuple[Any, Profile]) -> None:
        conn, profile = prepared
        rows = fb.pending(conn, profile.name)
        assert rows
        fb.save(conn, fb.Rating(profile.name, int(rows[0]["id"]), rating=1))
        remaining = fb.pending(conn, profile.name)
        assert len(remaining) == len(rows) - 1

    def test_best_first(self, prepared: tuple[Any, Profile]) -> None:
        conn, profile = prepared
        rows = fb.pending(conn, profile.name)
        scores = [r["final_score"] for r in rows]
        assert scores == sorted(scores, reverse=True)


class TestSave:
    def test_stores_rating_and_reason(self, prepared: tuple[Any, Profile]) -> None:
        conn, profile = prepared
        job_id = int(fb.pending(conn, profile.name)[0]["id"])
        fb.save(
            conn,
            fb.Rating(profile.name, job_id, rating=-1, reason="ort", score_seen=0.8),
        )
        row = conn.execute("SELECT * FROM feedback").fetchone()
        assert row["rating"] == -1
        assert row["reason"] == "ort"
        assert row["score_seen"] == pytest.approx(0.8)

    def test_rating_can_be_changed(self, prepared: tuple[Any, Profile]) -> None:
        conn, profile = prepared
        job_id = int(fb.pending(conn, profile.name)[0]["id"])
        fb.save(conn, fb.Rating(profile.name, job_id, rating=-1, reason="ort"))
        fb.save(conn, fb.Rating(profile.name, job_id, rating=1))
        rows = conn.execute("SELECT rating, reason FROM feedback").fetchall()
        assert len(rows) == 1
        assert rows[0]["rating"] == 1

    def test_profiles_are_independent(self, prepared: tuple[Any, Profile]) -> None:
        conn, profile = prepared
        job_id = int(fb.pending(conn, profile.name)[0]["id"])
        fb.save(conn, fb.Rating("test", job_id, rating=1))
        fb.save(conn, fb.Rating("anderer", job_id, rating=-1))
        assert conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 2


class TestReasons:
    def test_every_reason_maps_to_a_weight_or_is_deliberate(self) -> None:
        """Jeder Grund soll auf ein Gewicht zeigen — sonst nützt er nichts.

        Ausnahmen sind bewusst: "Arbeitgeber kommt nicht in Frage" und
        "anderer Grund" lassen sich nicht auf ein Gewicht abbilden.
        """
        ohne_gewicht = set(fb.REJECT_REASONS) - set(fb.REASON_TO_WEIGHT)
        assert ohne_gewicht == {"firma", "anderes"}

    def test_weights_referenced_exist(self) -> None:
        from jobpipe.config import ScoreWeights

        felder = set(ScoreWeights.model_fields)
        for weight in fb.REASON_TO_WEIGHT.values():
            assert weight in felder, f"{weight} ist kein Gewicht im Profil"

    def test_too_senior_points_at_the_seniority_weight(self) -> None:
        """Regression: der Grund zeigte auf ``keywords``.

        Das stammte aus der Zeit vor dem Anforderungsniveau-Kriterium. Ein
        höheres Keyword-Gewicht hätte "Teamleiter:in Administration" eher
        angehoben als gesenkt — die Kalibrierung hätte in die falsche Richtung
        gezogen, und zwar bei genau dem Grund, der für ein Einsteigerprofil am
        häufigsten vorkommt.
        """
        assert fb.REASON_TO_WEIGHT["senior"] == "seniority_penalty"


class TestStats:
    def test_counts_and_open(self, prepared: tuple[Any, Profile]) -> None:
        conn, profile = prepared
        rows = fb.pending(conn, profile.name)
        fb.save(conn, fb.Rating(profile.name, int(rows[0]["id"]), 1, score_seen=0.9))
        fb.save(conn, fb.Rating(profile.name, int(rows[1]["id"]), -1, "ort", score_seen=0.5))
        s = fb.stats(conn, profile.name)
        assert s["bewertet"] == 2
        assert s["offen"] == len(rows) - 2
        assert s["nach_bewertung"]["interessant"] == 1
        assert "zu weit weg" in s["ablehnungsgründe"]

    def test_calibration_threshold(self, prepared: tuple[Any, Profile]) -> None:
        conn, profile = prepared
        assert fb.stats(conn, profile.name)["reicht_für_kalibrierung"] is False

    def test_separation_measures_score_gap(self, prepared: tuple[Any, Profile]) -> None:
        """Die einfachste sinnvolle Kennzahl vor der Kalibrierung.

        Liegt der Schnitt der interessanten Treffer nicht über dem der
        abgelehnten, hilft kein Feintuning — dann stimmt etwas Grundsätzliches
        nicht.
        """
        conn, profile = prepared
        rows = fb.pending(conn, profile.name)
        fb.save(conn, fb.Rating(profile.name, int(rows[0]["id"]), 1, score_seen=0.90))
        fb.save(conn, fb.Rating(profile.name, int(rows[1]["id"]), 1, score_seen=0.80))
        fb.save(conn, fb.Rating(profile.name, int(rows[2]["id"]), -1, "ort", score_seen=0.40))
        sep = fb.separation(conn, profile.name)
        assert sep["schnitt_interessant"] == pytest.approx(0.85)
        assert sep["schnitt_nein"] == pytest.approx(0.40)
        assert sep["abstand"] == pytest.approx(0.45)

    def test_separation_without_data(self, tmp_path: Path) -> None:
        conn = db.init_db(tmp_path / "t.db")
        sep = fb.separation(conn, "leer")
        assert sep["abstand"] is None
