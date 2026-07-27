"""Konfiguration, Profile und Storage."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import yaml

from jobpipe.config import Config, Profile
from jobpipe.store import db

REPO = Path(__file__).resolve().parents[1]


class TestConfig:
    def test_repo_config_is_valid(self) -> None:
        cfg = Config.load(REPO / "config.yaml")
        assert cfg.region.cantons == ["ZH", "SG", "TG"]
        assert cfg.sources.job_room.enabled

    def test_disabling_robots_is_rejected(self) -> None:
        """`respect_robots: false` darf es nicht geben.

        Wenn eine Quelle uns aussperrt, ist das eine Antwort und kein Schalter.
        """
        with pytest.raises(ValueError, match="respect_robots"):
            Config.model_validate({"politeness": {"respect_robots": False}})

    def test_rate_limit_upper_bound(self) -> None:
        with pytest.raises(ValueError):
            Config.model_validate({"politeness": {"requests_per_second": 50}})

    def test_user_agent_carries_contact(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JOBPIPE_CONTACT_URL", "https://github.com/x/y")
        ua = Config().politeness.resolved_user_agent()
        assert "jobpipe/" in ua
        assert "https://github.com/x/y" in ua

    def test_user_agent_is_honest_without_contact(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("JOBPIPE_CONTACT_URL", raising=False)
        ua = Config().politeness.resolved_user_agent()
        assert "jobpipe/" in ua  # kein Browser-Spoofing als Fallback
        assert "Mozilla" not in ua

    def test_page_size_capped_at_server_maximum(self) -> None:
        with pytest.raises(ValueError):
            Config.model_validate({"sources": {"job_room": {"page_size": 1000}}})


class TestProfile:
    def test_example_profile_loads(self) -> None:
        p = Profile.load("example", REPO / "profiles")
        assert p.display_name
        assert len(p.target_roles) >= 2

    def test_missing_profile_lists_alternatives(self, tmp_path: Path) -> None:
        (tmp_path / "vorhanden.yaml").write_text("display_name: X\n", encoding="utf-8")
        with pytest.raises(FileNotFoundError, match="vorhanden"):
            Profile.load("gibtsnicht", tmp_path)

    def test_workload_range_is_validated(self) -> None:
        with pytest.raises(ValueError):
            Profile.model_validate(
                {"name": "x", "display_name": "X", "workload_min": 80, "workload_max": 50}
            )

    def test_exclusions_are_supported(self, tmp_path: Path) -> None:
        """Ausschlusskriterien sind kein Beiwerk.

        Wer sich wegbewerben will, braucht sie: der eigene CV zieht sonst genau
        die Stellen an, die man verlassen möchte.
        """
        (tmp_path / "p.yaml").write_text(
            yaml.safe_dump(
                {
                    "display_name": "Test",
                    "keywords_exclude": ["Empfang", "Frontdesk", "Kundenkontakt"],
                    "target_roles": [{"name": "Backoffice", "description": "Innendienst"}],
                }
            ),
            encoding="utf-8",
        )
        p = Profile.load("p", tmp_path)
        assert "Frontdesk" in p.keywords_exclude
        assert p.target_roles[0].name == "Backoffice"


class TestStore:
    @pytest.fixture
    def conn(self, tmp_path: Path) -> sqlite3.Connection:
        return db.init_db(tmp_path / "t.db")

    def test_schema_created(self, conn: sqlite3.Connection) -> None:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"jobs", "runs", "berufe", "plz_coords"} <= tables

    def test_migration_is_idempotent(self, conn: sqlite3.Connection) -> None:
        assert db.migrate(conn) == db.SCHEMA_VERSION
        assert db.migrate(conn) == db.SCHEMA_VERSION

    def test_same_ad_cannot_be_inserted_twice(self, conn: sqlite3.Connection) -> None:
        sql = (
            "INSERT INTO jobs (portal, source_id, source_url, title, company_name,"
            " first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?)"
        )
        args = ("job_room", "id-1", "https://x", "T", "C", "now", "now")
        conn.execute(sql, args)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql, args)

    def test_run_lifecycle(self, conn: sqlite3.Connection) -> None:
        rid = db.start_run(conn, "job_room", {"since": 7})
        assert db.last_successful_run(conn, "job_room") is None  # noch läuft er
        db.finish_run(conn, rid, items_seen=6020, requests_made=14)
        row = db.last_successful_run(conn, "job_room")
        assert row is not None
        assert row["items_seen"] == 6020
        assert row["requests_made"] == 14

    def test_failed_run_records_error(self, conn: sqlite3.Connection) -> None:
        rid = db.start_run(conn, "job_room", {})
        db.finish_run(conn, rid, status="failed", error="Fenster überschritten")
        row = conn.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
        assert row["status"] == "failed"
        assert "Fenster" in row["error"]
        assert db.last_successful_run(conn, "job_room") is None
