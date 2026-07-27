"""Embeddings, Regeln und Gesamtscore."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from jobpipe.config import LocationPreference, Profile, ScoreWeights, TargetRole
from jobpipe.enrich import rules
from jobpipe.enrich.embed import (
    EmbeddingCache,
    EmbeddingError,
    FallbackEmbedder,
    build_job_text,
    cache_key,
    cosine_max,
    l2_normalize,
    spread_scores,
    truncate,
)
from jobpipe.parse.schema import JobPosting
from jobpipe.store import db


def make_job(**kw: Any) -> JobPosting:
    base: dict[str, Any] = {
        "portal": "job_room",
        "source_id": "x",
        "source_url": "https://example.ch/1",
        "title": "Sachbearbeiter/in Innendienst 80-100%",
        "company_name": "Muster AG",
    }
    return JobPosting(**(base | kw))


def make_profile(**kw: Any) -> Profile:
    base: dict[str, Any] = {
        "name": "test",
        "display_name": "Test",
        "workload_min": 80,
        "workload_max": 100,
        "locations": [LocationPreference(label="Thundorf", lat=47.5525, lon=8.9317, radius_km=25)],
    }
    return Profile(**(base | kw))


# --------------------------------------------------------------------------


class TestEmbeddingHelpers:
    def test_l2_normalize(self) -> None:
        v = l2_normalize(np.array([3.0, 4.0], dtype="float32"))
        assert float(np.linalg.norm(v)) == pytest.approx(1.0)

    def test_l2_normalize_handles_zero_vector(self) -> None:
        v = l2_normalize(np.zeros(4, dtype="float32"))
        assert not np.isnan(v).any()

    def test_matryoshka_truncation_needs_renormalisation(self) -> None:
        """Gemini liefert bei gekürzter Dimension Vektoren mit Norm ~0.59.

        Ohne Nachnormalisierung ist das Skalarprodukt keine Cosine mehr und
        alle Scores sind verzerrt.
        """
        raw = np.array([0.3, 0.4, 0.1], dtype="float32")
        assert float(np.linalg.norm(raw)) < 1.0
        assert float(np.linalg.norm(l2_normalize(raw))) == pytest.approx(1.0)

    def test_truncate_limits_length(self) -> None:
        assert len(truncate("x" * 10_000, 100)) == 100

    def test_cache_key_depends_on_model_and_dim(self) -> None:
        a = cache_key("m1", 768, "text")
        assert a != cache_key("m2", 768, "text")
        assert a != cache_key("m1", 1536, "text")
        assert a == cache_key("m1", 768, "text")

    def test_job_text_repeats_title(self) -> None:
        """Der Titel trägt das meiste Signal und darf im Fliesstext nicht untergehen."""
        text = build_job_text("Data Analyst", "Muster AG", "Lange Beschreibung.")
        assert text.count("Data Analyst") >= 2

    def test_cosine_max_picks_best_role(self) -> None:
        job = l2_normalize(np.array([1.0, 0.0], dtype="float32"))
        roles = [
            l2_normalize(np.array([0.0, 1.0], dtype="float32")),
            l2_normalize(np.array([1.0, 0.1], dtype="float32")),
        ]
        sim, idx = cosine_max(job, roles)
        assert idx == 1
        assert sim > 0.9

    def test_cosine_max_without_roles(self) -> None:
        assert cosine_max(np.array([1.0]), []) == (0.0, -1)


class TestSpreadScores:
    def test_narrow_band_becomes_full_range(self) -> None:
        """Deutsche Bürotexte liegen im Cosine eng beieinander.

        Gemessen: "Sachbearbeiterin Innendienst" gegen "Empfangsmitarbeiterin
        Frontdesk" ergibt 0.86, obwohl das genau die Unterscheidung ist, die
        zählt. Roh verwendet wäre der semantische Anteil praktisch konstant.
        """
        out = spread_scores([0.81, 0.83, 0.85, 0.87])
        assert min(out) == 0.0
        assert max(out) == 1.0

    def test_order_is_preserved(self) -> None:
        values = [0.5, 0.9, 0.1, 0.7]
        out = spread_scores(values)
        assert sorted(range(4), key=lambda i: values[i]) == sorted(range(4), key=lambda i: out[i])

    def test_edge_cases(self) -> None:
        assert spread_scores([]) == []
        assert spread_scores([0.42]) == [1.0]


class TestFallbackEmbedder:
    def test_switches_on_quota_error(self) -> None:
        """Kontingentende darf den Lauf nicht abbrechen.

        Gemessen kam beim kostenlosen Gemini-Tier bereits im zweiten Batch ein
        429. Die Pipeline soll dann mit dem lokalen Modell weiterrechnen.
        """

        class Failing:
            name = "gemini"
            dim = 768

            def embed(self, texts: Any) -> list[np.ndarray]:
                raise EmbeddingError("429 RESOURCE_EXHAUSTED")

        class Working:
            name = "local"
            dim = 4

            def embed(self, texts: Any) -> list[np.ndarray]:
                return [np.ones(4, dtype="float32") for _ in texts]

        fb = FallbackEmbedder(Failing(), Working)  # type: ignore[arg-type]
        out = fb.embed(["a", "b"])
        assert len(out) == 2
        assert fb.switched
        assert fb.name == "local"

    def test_stays_on_primary_when_it_works(self) -> None:
        class Working:
            name = "gemini"
            dim = 4

            def embed(self, texts: Any) -> list[np.ndarray]:
                return [np.zeros(4, dtype="float32") for _ in texts]

        fb = FallbackEmbedder(Working(), Working)  # type: ignore[arg-type]
        fb.embed(["a"])
        assert not fb.switched


class TestEmbeddingCache:
    def test_second_call_hits_cache(self, tmp_path: Path) -> None:
        conn = db.init_db(tmp_path / "t.db")

        class Counting:
            name = "fake"
            dim = 4
            calls = 0

            def embed(self, texts: Any) -> list[np.ndarray]:
                Counting.calls += 1
                return [np.full(4, 0.5, dtype="float32") for _ in texts]

        cache = EmbeddingCache(conn, Counting())  # type: ignore[arg-type]
        cache.embed(["a", "b"])
        assert Counting.calls == 1
        cache.embed(["a", "b"])
        assert Counting.calls == 1, "zweiter Aufruf muss aus dem Cache kommen"
        assert cache.stats.from_cache == 2

    def test_partial_cache_hit(self, tmp_path: Path) -> None:
        conn = db.init_db(tmp_path / "t.db")
        seen: list[list[str]] = []

        class Recording:
            name = "fake"
            dim = 4

            def embed(self, texts: Any) -> list[np.ndarray]:
                seen.append(list(texts))
                return [np.full(4, 0.5, dtype="float32") for _ in texts]

        cache = EmbeddingCache(conn, Recording())  # type: ignore[arg-type]
        cache.embed(["a"])
        cache.embed(["a", "b"])
        assert seen[1] == ["b"], "nur der fehlende Text darf neu berechnet werden"


class TestRules:
    def test_workload_overlap(self) -> None:
        p = make_profile()
        assert rules.score_workload(make_job(workload_min=80, workload_max=100), p)[0] == 1.0
        assert rules.score_workload(make_job(workload_min=20, workload_max=40), p)[0] == 0.0

    def test_workload_unknown_is_neutral(self) -> None:
        v, why = rules.score_workload(make_job(), make_profile())
        assert v == 0.5
        assert "nicht angegeben" in why

    def test_location_full_inside_radius_then_decays(self) -> None:
        p = make_profile()
        near = make_job(lat=47.556, lon=8.898)  # Frauenfeld, ~5 km
        far = make_job(lat=47.3769, lon=8.5417)  # Zürich, ~35 km
        very_far = make_job(lat=46.9480, lon=7.4474)  # Bern
        assert rules.score_location(near, p)[0] == 1.0
        assert 0.0 < rules.score_location(far, p)[0] < 1.0
        assert rules.score_location(very_far, p)[0] == 0.0

    def test_flextime_is_bonus_not_filter(self) -> None:
        """Gleitzeit steht in 1.2 % der Inserate.

        Ein harter Filter würde 98 % wegwerfen, die meisten davon zu Unrecht.
        Ohne Angabe bleibt deshalb ein spürbarer Restwert.
        """
        p = make_profile(wants_flextime=True)
        with_flex = make_job(description_md="Wir bieten gleitende Arbeitszeiten.")
        without = make_job(description_md="Wir bieten ein gutes Team.")
        assert rules.score_flextime(with_flex, p)[0] == 1.0
        assert rules.score_flextime(without, p)[0] > 0.0

    def test_flextime_quotes_the_evidence(self) -> None:
        p = make_profile(wants_flextime=True)
        _, why = rules.score_flextime(make_job(description_md="Wir bieten Gleitzeit an."), p)
        assert "Gleitzeit" in why

    def test_exclusion_in_title_weighs_more_than_in_body(self) -> None:
        """ "Empfangsmitarbeiterin" im Titel beschreibt die Stelle.

        "Empfang" irgendwo im Text ist oft eine Randnotiz.
        """
        p = make_profile(keywords_exclude=["Empfang"])
        in_title = make_job(title="Empfangsmitarbeiterin 80%")
        in_body = make_job(description_md="Gelegentliche Aushilfe am Empfang.")
        assert rules.score_exclusions(in_title, p)[0] > rules.score_exclusions(in_body, p)[0]

    def test_no_exclusion_means_no_penalty(self) -> None:
        p = make_profile(keywords_exclude=["Empfang"])
        assert rules.score_exclusions(make_job(), p)[0] == 0.0

    def test_keywords_saturate(self) -> None:
        p = make_profile(keywords_positive=["SQL", "Python", "Data", "BI", "ETL"])
        one = make_job(description_md="Wir nutzen SQL.")
        many = make_job(description_md="SQL Python Data BI ETL")
        v1 = rules.score_keywords(one, p)[0]
        v5 = rules.score_keywords(many, p)[0]
        assert v1 < v5 <= 1.0

    def test_recency_decays(self) -> None:
        fresh = make_job(posted_at=datetime.now(UTC))
        old = make_job(posted_at=datetime.now(UTC) - timedelta(days=60))
        assert rules.score_recency(fresh)[0] > rules.score_recency(old)[0]

    def test_agency_penalty(self) -> None:
        assert rules.score_agency(make_job(company_is_agency=True))[0] == 1.0
        assert rules.score_agency(make_job(company_is_agency=False))[0] == 0.0

    def test_evaluate_returns_all_components(self) -> None:
        res = rules.evaluate(make_job(lat=47.556, lon=8.898), make_profile())
        for key in (
            "workload",
            "location",
            "flextime",
            "home_office",
            "keywords",
            "recency",
            "exclude_penalty",
            "agency_penalty",
        ):
            assert key in res.scores
        assert res.distance_km is not None


class TestScoringIntegration:
    def test_frontdesk_ranks_below_backoffice(self, tmp_path: Path) -> None:
        """Der Kernfall von Profil B.

        Der Lebenslauf klingt nach Frontdesk, gesucht ist Backoffice. Ohne
        Ausschlusskriterien zieht das Profil genau die Stellen an, die
        vermieden werden sollen.
        """
        from jobpipe.enrich.score import run
        from jobpipe.store import jobs as jobs_store

        conn = db.init_db(tmp_path / "t.db")
        wanted = make_job(
            source_id="1",
            title="Sachbearbeiterin Innendienst 80-100%",
            description_md="Auftragsabwicklung, Fakturierung, Stammdatenpflege im Büro.",
            lat=47.556,
            lon=8.898,
            workload_min=80,
            workload_max=100,
            posted_at=datetime.now(UTC),
        )
        unwanted = make_job(
            source_id="2",
            title="Empfangsmitarbeiterin Frontdesk 80-100%",
            description_md="Empfang von Kundinnen und Kunden, Telefonzentrale, Schalterdienst.",
            lat=47.556,
            lon=8.898,
            workload_min=80,
            workload_max=100,
            posted_at=datetime.now(UTC),
        )
        jobs_store.upsert_many(conn, [wanted, unwanted])

        profile = make_profile(
            target_roles=[
                TargetRole(
                    name="Sachbearbeitung Innendienst",
                    description="Auftragsabwicklung, Fakturierung, Stammdaten, Arbeit im Büro.",
                )
            ],
            keywords_positive=["Sachbearbeit", "Innendienst", "Auftragsabwicklung"],
            keywords_exclude=["Empfang", "Frontdesk", "Telefonzentrale", "Schalter"],
            weights=ScoreWeights(exclude_penalty=1.2),
        )
        report = run(conn, profile, prefer_local=True, top_n=2)
        assert report.scored == 2
        assert report.top[0]["title"].startswith("Sachbearbeiterin")

    def test_scores_are_persisted_with_breakdown(self, tmp_path: Path) -> None:
        from jobpipe.enrich.score import run
        from jobpipe.store import jobs as jobs_store

        conn = db.init_db(tmp_path / "t.db")
        jobs_store.upsert_many(conn, [make_job(lat=47.556, lon=8.898)])
        profile = make_profile(
            target_roles=[TargetRole(name="R", description="Sachbearbeitung im Innendienst.")]
        )
        run(conn, profile, prefer_local=True)

        row = conn.execute("SELECT * FROM scores").fetchone()
        assert row is not None
        assert row["profile"] == "test"
        assert row["best_role"] == "R"
        assert "scores" in row["rules_json"]
        assert row["semantic_raw"] is not None

    def test_rerun_replaces_previous_scores(self, tmp_path: Path) -> None:
        from jobpipe.enrich.score import run
        from jobpipe.store import jobs as jobs_store

        conn = db.init_db(tmp_path / "t.db")
        jobs_store.upsert_many(conn, [make_job(lat=47.556, lon=8.898)])
        profile = make_profile(target_roles=[TargetRole(name="R", description="Sachbearbeitung.")])
        run(conn, profile, prefer_local=True)
        run(conn, profile, prefer_local=True)
        assert conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 1
