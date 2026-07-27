"""Tests für Drosselung, robots.txt-Gate und den job-room-Fetcher.

Alle HTTP-Aufrufe sind mit respx gemockt — die Suite läuft offline und fasst
die Portale nicht an.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from jobpipe.collect.base import (
    PoliteClient,
    RateLimiter,
    RawCache,
    RobotsDisallowed,
    RobotsGate,
)
from jobpipe.collect.job_room import JobRoomFetcher, JobRoomQuery, ResultWindowExceeded
from jobpipe.config import Config

FIXTURES = Path(__file__).parent / "fixtures"
UA = "jobpipe/0.1 (test; +https://example.com)"

ROBOTS_JOBROOM = """# Do not crawl Job Adverts
User-agent: *
User-agent: AdsBot-Google
Disallow: /job-search/

User-agent: *
Disallow: /aav/confirmation
"""

ROBOTS_OSTJOB = """User-agent: x28-job-bot
Disallow: /
User-agent: JobRoboter
Disallow: /
User-agent: *
Allow: /
Sitemap: http://www.ostjob.ch/sitemap.xml
Disallow: /api
Disallow: /job/preview
"""


class TestRateLimiter:
    def test_spaces_requests_out(self) -> None:
        rl = RateLimiter(20.0)  # 50 ms Abstand
        start = time.monotonic()
        for _ in range(4):
            rl.acquire("host")
        assert time.monotonic() - start >= 0.15

    def test_hosts_are_independent(self) -> None:
        rl = RateLimiter(2.0)
        rl.acquire("a.ch")
        start = time.monotonic()
        rl.acquire("b.ch")  # anderer Host -> kein Warten
        assert time.monotonic() - start < 0.1

    def test_rejects_invalid_rate(self) -> None:
        with pytest.raises(ValueError):
            RateLimiter(0)


class TestRobotsGate:
    @respx.mock
    def test_allows_and_blocks_job_room_paths(self) -> None:
        respx.get("https://www.job-room.ch/robots.txt").mock(
            return_value=httpx.Response(200, text=ROBOTS_JOBROOM)
        )
        gate = RobotsGate(UA)
        assert gate.allows("https://www.job-room.ch/jobadservice/api/jobAdvertisements/_search")
        assert not gate.allows("https://www.job-room.ch/job-search/abc")

    @respx.mock
    def test_ostjob_api_is_blocked_details_are_not(self) -> None:
        respx.get("https://www.ostjob.ch/robots.txt").mock(
            return_value=httpx.Response(200, text=ROBOTS_OSTJOB)
        )
        gate = RobotsGate(UA)
        assert gate.allows("https://www.ostjob.ch/job/titel/123")
        assert not gate.allows("https://www.ostjob.ch/api/vacancies")
        assert not gate.allows("https://www.ostjob.ch/job/preview/9")

    @respx.mock
    def test_check_raises_on_disallow(self) -> None:
        respx.get("https://www.job-room.ch/robots.txt").mock(
            return_value=httpx.Response(200, text=ROBOTS_JOBROOM)
        )
        gate = RobotsGate(UA)
        with pytest.raises(RobotsDisallowed):
            gate.check("https://www.job-room.ch/job-search/x")

    @respx.mock
    def test_missing_robots_means_allowed(self) -> None:
        respx.get("https://example.ch/robots.txt").mock(return_value=httpx.Response(404))
        assert RobotsGate(UA).allows("https://example.ch/anything")

    @respx.mock
    def test_unreachable_robots_aborts_instead_of_assuming_allowed(self) -> None:
        respx.get("https://example.ch/robots.txt").mock(side_effect=httpx.ConnectError("down"))
        with pytest.raises(RobotsDisallowed):
            RobotsGate(UA).allows("https://example.ch/x")

    @respx.mock
    def test_robots_is_cached(self) -> None:
        route = respx.get("https://example.ch/robots.txt").mock(
            return_value=httpx.Response(200, text="User-agent: *\nDisallow:")
        )
        gate = RobotsGate(UA, cache_minutes=60)
        for _ in range(5):
            gate.allows("https://example.ch/a")
        assert route.call_count == 1


class TestPoliteClient:
    @respx.mock
    def test_sends_our_user_agent(self) -> None:
        respx.get("https://example.ch/robots.txt").mock(
            return_value=httpx.Response(200, text="User-agent: *\nDisallow:")
        )
        route = respx.get("https://example.ch/data").mock(return_value=httpx.Response(200, json={}))
        with PoliteClient(user_agent=UA, requests_per_second=100) as c:
            c.get("https://example.ch/data")
        assert route.calls[0].request.headers["user-agent"] == UA

    @respx.mock
    def test_refuses_disallowed_path(self) -> None:
        respx.get("https://www.job-room.ch/robots.txt").mock(
            return_value=httpx.Response(200, text=ROBOTS_JOBROOM)
        )
        with (
            PoliteClient(user_agent=UA, requests_per_second=100) as c,
            pytest.raises(RobotsDisallowed),
        ):
            c.get("https://www.job-room.ch/job-search/x")

    @respx.mock
    def test_dry_run_sends_nothing(self) -> None:
        respx.get("https://example.ch/robots.txt").mock(
            return_value=httpx.Response(200, text="User-agent: *\nDisallow:")
        )
        route = respx.get("https://example.ch/data").mock(return_value=httpx.Response(200))
        with PoliteClient(user_agent=UA, dry_run=True) as c:
            c.get("https://example.ch/data")
            assert c.planned == [("GET", "https://example.ch/data")]
        assert route.call_count == 0


class TestJobRoomFetcher:
    @pytest.fixture
    def page(self) -> list[dict[str, Any]]:
        return json.loads((FIXTURES / "job_room_search_page.json").read_text(encoding="utf-8"))

    def _mock_robots(self) -> None:
        respx.get("https://www.job-room.ch/robots.txt").mock(
            return_value=httpx.Response(200, text=ROBOTS_JOBROOM)
        )

    @respx.mock
    def test_fetches_and_caches_raw(self, tmp_path: Path, page: list[dict[str, Any]]) -> None:
        self._mock_robots()
        respx.post(url__startswith="https://www.job-room.ch/jobadservice/api").mock(
            return_value=httpx.Response(200, json=page, headers={"x-total-count": str(len(page))})
        )
        cfg = Config()
        cache = RawCache(tmp_path)
        with PoliteClient(user_agent=UA, requests_per_second=100) as c:
            res = JobRoomFetcher(cfg, c, cache).fetch(since_days=1)

        assert res.items_seen == len(page)
        assert len(res.raw_paths) == len(page)
        assert all(p.exists() for p in res.raw_paths)

    @respx.mock
    def test_cached_records_carry_no_pii(self, tmp_path: Path, page: list[dict[str, Any]]) -> None:
        from jobpipe.parse.pii import find_contact_leaks

        self._mock_robots()
        respx.post(url__startswith="https://www.job-room.ch/jobadservice/api").mock(
            return_value=httpx.Response(200, json=page, headers={"x-total-count": str(len(page))})
        )
        cache = RawCache(tmp_path)
        with PoliteClient(user_agent=UA, requests_per_second=100) as c:
            res = JobRoomFetcher(Config(), c, cache).fetch(since_days=1)

        for p in res.raw_paths:
            assert find_contact_leaks(cache.read(p)) == []

    @respx.mock
    def test_refuses_oversized_query_before_sending(self, tmp_path: Path) -> None:
        """Bei >10'000 Treffern antwortet der Server mit HTTP 412.

        Wir fangen das vorher ab und sagen, was zu tun ist.
        """
        self._mock_robots()
        respx.post(url__startswith="https://www.job-room.ch/jobadservice/api").mock(
            return_value=httpx.Response(200, json=[], headers={"x-total-count": "19216"})
        )
        with PoliteClient(user_agent=UA, requests_per_second=100) as c:
            fetcher = JobRoomFetcher(Config(), c, RawCache(tmp_path))
            with pytest.raises(ResultWindowExceeded, match="19216"):
                fetcher.fetch(since_days=30)

    @respx.mock
    def test_empty_result_is_not_an_error(self, tmp_path: Path) -> None:
        self._mock_robots()
        respx.post(url__startswith="https://www.job-room.ch/jobadservice/api").mock(
            return_value=httpx.Response(200, json=[], headers={"x-total-count": "0"})
        )
        with PoliteClient(user_agent=UA, requests_per_second=100) as c:
            res = JobRoomFetcher(Config(), c, RawCache(tmp_path)).fetch(since_days=1)
        assert res.items_seen == 0

    def test_query_body_matches_api_contract(self) -> None:
        body = JobRoomQuery(since_days=7, cantons=["ZH", "SG", "TG"]).to_body()
        assert body["onlineSince"] == 7
        assert body["cantonCodes"] == ["ZH", "SG", "TG"]
        # Inserate nur für RAV-Beratende gehen uns nichts an
        assert body["displayRestricted"] is False
