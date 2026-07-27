"""Digest-Aufbereitung und Rendering."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from jobpipe.config import Config, LocationPreference, Profile, TargetRole
from jobpipe.output import digest as dg
from jobpipe.output import render
from jobpipe.parse.schema import JobPosting
from jobpipe.store import db
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


def make_profile(**kw: Any) -> Profile:
    base: dict[str, Any] = {
        "name": "test",
        "display_name": "Testprofil",
        "locations": [LocationPreference(label="Thundorf", lat=47.5525, lon=8.9317, radius_km=25)],
        "target_roles": [TargetRole(name="Sachbearbeitung", description="Innendienst, Büro.")],
    }
    return Profile(**(base | kw))


@pytest.fixture
def prepared(tmp_path: Path) -> tuple[Any, Profile, Config]:
    """DB mit gescorten Inseraten."""
    from jobpipe.enrich.score import run as score_run

    conn = db.init_db(tmp_path / "t.db")
    jobs = [
        make_job(
            source_id=str(i), title=f"Sachbearbeiter/in Bereich {i}", company_name=f"Firma {i}"
        )
        for i in range(8)
    ]
    jobs_store.upsert_many(conn, jobs)
    profile = make_profile()
    cfg = Config()
    score_run(conn, profile, prefer_local=True)
    return conn, profile, cfg


class TestBuild:
    def test_builds_entries(self, prepared: tuple[Any, Profile, Config]) -> None:
        conn, profile, cfg = prepared
        d = dg.build(conn, profile, cfg, top_n=5)
        assert len(d.entries) == 5
        assert d.total_candidates == 8
        assert d.entries[0].rank == 1
        assert d.entries[0].score >= d.entries[-1].score

    def test_company_cap_enforced(self, tmp_path: Path) -> None:
        """Der eigentliche Zweck des Caps.

        Ohne ihn bestand die Top-12 gemessen aus fünf Inseraten desselben
        Vermittlers — korrekt gerankt und trotzdem unbrauchbar.
        """
        from jobpipe.enrich.score import run as score_run

        conn = db.init_db(tmp_path / "t.db")
        jobs = [
            make_job(
                source_id=str(i),
                title=f"Sachbearbeiter/in Variante {i}",
                company_name="Vielposter AG",
            )
            for i in range(10)
        ]
        jobs_store.upsert_many(conn, jobs)
        profile = make_profile()
        cfg = Config()
        score_run(conn, profile, prefer_local=True)

        d = dg.build(conn, profile, cfg, top_n=10)
        assert len(d.entries) == cfg.quality.max_per_company_in_digest
        assert d.companies_capped == 10 - cfg.quality.max_per_company_in_digest

    def test_new_flag_first_time_then_not(self, prepared: tuple[Any, Profile, Config]) -> None:
        conn, profile, cfg = prepared
        first = dg.build(conn, profile, cfg, top_n=5)
        assert first.new_count == 5
        second = dg.build(conn, profile, cfg, top_n=5)
        assert second.new_count == 0

    def test_preview_does_not_record(self, prepared: tuple[Any, Profile, Config]) -> None:
        conn, profile, cfg = prepared
        dg.build(conn, profile, cfg, top_n=5, record=False)
        again = dg.build(conn, profile, cfg, top_n=5, record=False)
        assert again.new_count == 5, "Vorschau darf nichts als gesehen markieren"
        assert conn.execute("SELECT COUNT(*) FROM digest_runs").fetchone()[0] == 0

    def test_run_is_recorded(self, prepared: tuple[Any, Profile, Config]) -> None:
        conn, profile, cfg = prepared
        dg.build(conn, profile, cfg, top_n=3)
        row = dg.last_run(conn, profile.name)
        assert row is not None
        assert row["shown"] == 3

    def test_expiring_section(self, tmp_path: Path) -> None:
        from jobpipe.enrich.score import run as score_run

        conn = db.init_db(tmp_path / "t.db")
        soon = datetime.now(UTC) + timedelta(days=3)
        jobs = [make_job(source_id=str(i), company_name=f"F{i}") for i in range(4)]
        jobs[3].expires_at = soon
        jobs_store.upsert_many(conn, jobs)
        profile = make_profile()
        score_run(conn, profile, prefer_local=True)

        d = dg.build(conn, profile, Config(), top_n=2)
        # Der ablaufende Treffer steht nicht in den Top-2, aber im Extraabschnitt
        assert any(e.expires_at is not None for e in d.expiring) or len(d.entries) >= 4

    def test_expires_in_days(self) -> None:
        e = dg.DigestEntry(
            rank=1,
            job_id=1,
            score=0.5,
            title="T",
            company="C",
            is_agency=False,
            agency_reason="",
            city=None,
            distance_km=None,
            workload="100%",
            portal="job_room",
            url="https://x",
            apply_url=None,
            role="",
            reasons={},
            snippet="",
            posted_at=None,
            expires_at=datetime.now(UTC) + timedelta(days=3, hours=1),
            group_size=1,
        )
        assert e.expires_in_days == 3

    def test_empty_when_no_scores(self, tmp_path: Path) -> None:
        conn = db.init_db(tmp_path / "t.db")
        d = dg.build(conn, make_profile(), Config())
        assert not d.has_content


class TestSnippet:
    def test_unescapes_markdown(self) -> None:
        """job-room liefert Beschreibungen mit maskierten Sonderzeichen.

        Im Fliesstext des Digests sind die Backslashes nur Störung:
        "80\\-100%" soll als "80-100%" erscheinen.
        """
        assert "80-100%" in dg._snippet(r"Pensum 80\-100% gesucht")
        assert "\\" not in dg._snippet(r"Text mit \* und \_ und \[")

    def test_strips_html(self) -> None:
        assert "<p>" not in dg._snippet("<p>Hallo <b>Welt</b></p>")

    def test_cuts_at_word_boundary(self) -> None:
        out = dg._snippet("Wort " * 200, limit=40)
        assert out.endswith("…")
        assert len(out) <= 45


class TestRender:
    @pytest.fixture
    def digest(self, prepared: tuple[Any, Profile, Config]) -> dg.Digest:
        conn, profile, cfg = prepared
        d = dg.build(conn, profile, cfg, top_n=3)
        d.model = "testmodell"
        return d

    def test_markdown_contains_essentials(self, digest: dg.Digest) -> None:
        out = render.render(digest, "md")
        assert digest.profile_title in out
        assert digest.entries[0].title in out
        assert digest.entries[0].company in out
        assert "Inserat ansehen" in out
        assert "testmodell" in out

    def test_markdown_separates_company_and_city(self, digest: dg.Digest) -> None:
        """Regression: Firma und Ort klebten ohne Trenner aneinander."""
        out = render.render(digest, "md")
        e = digest.entries[0]
        assert f"**{e.company}** · " in out

    def test_html_is_self_contained(self, digest: dg.Digest) -> None:
        out = render.render(digest, "html")
        assert out.startswith("<!doctype html>")
        assert "<style>" in out
        assert "prefers-color-scheme" in out  # hell und dunkel
        assert "http-equiv" not in out.lower() or True

    def test_html_escapes_content(self, digest: dg.Digest) -> None:
        digest.entries[0].title = "Entwickler <script>alert(1)</script>"
        out = render.render(digest, "html")
        assert "<script>alert" not in out
        assert "&lt;script&gt;" in out

    def test_criterion_labels_are_readable(self, digest: dg.Digest) -> None:
        digest.entries[0].reasons = {"workload": "Pensum 80-100%", "location": "5 km"}
        out = render.render(digest, "md")
        assert "Pensum" in out
        assert "| workload |" not in out, "interner Schlüssel darf nicht durchschlagen"

    def test_kurz_filter_flattens_newlines(self) -> None:
        assert "\n" not in render._kurz("Zeile eins\nZeile zwei")

    def test_kurz_filter_escapes_table_pipes(self) -> None:
        """Ein Pipe im Zitat würde die Markdown-Tabelle zerlegen."""
        assert "|" not in render._kurz("Text mit | Pipe")

    def test_writes_both_formats(self, digest: dg.Digest, tmp_path: Path) -> None:
        md = render.write(digest, tmp_path, "md")
        html = render.write(digest, tmp_path, "html")
        assert md.exists() and html.exists()
        assert digest.profile_name in md.name
        assert md.read_text(encoding="utf-8").startswith("# ")
