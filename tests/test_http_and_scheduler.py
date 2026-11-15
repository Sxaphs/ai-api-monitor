from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from app.collectors.http import FetchError, Fetcher
from app.config import Settings
from app.scheduler import create_scheduler, set_site_collection_hours


def config(attempts=3):
    return Settings(
        database_url="sqlite://", rapidapi_key=None, scheduler_enabled=False,
        log_level="INFO", request_timeout=30, retry_attempts=attempts, retry_backoff=0.5,
    )


def test_http_retries_backoff_and_rotates_user_agent():
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) < 3:
            return httpx.Response(500)
        return httpx.Response(200, text="ok")

    sleeps = []
    fetcher = Fetcher(config(), sleep=sleeps.append, transport=httpx.MockTransport(handler))
    result = fetcher.fetch("https://example.com")
    assert result.text == "ok"
    assert len(requests) == 3
    assert sleeps == [0.5, 1.0]
    assert len({request.headers["user-agent"] for request in requests}) == 3
    assert fetcher.timeout == 30


def test_http_failure_after_three_attempts():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503)

    fetcher = Fetcher(config(), sleep=lambda _: None, transport=httpx.MockTransport(handler))
    with pytest.raises(FetchError):
        fetcher.fetch("https://example.com")
    assert len(calls) == 3


def test_scheduler_only_has_collection_jobs(session_factory):
    scheduler = create_scheduler(session_factory)
    jobs = {job.id: job for job in scheduler.get_jobs()}
    assert set(jobs) == {"collect-sites", "collect-x-2h"}
    assert "hour='2,8,14,20'" in str(jobs["collect-sites"].trigger)
    assert "minute='0'" in str(jobs["collect-sites"].trigger)
    assert jobs["collect-x-2h"].trigger.interval == timedelta(hours=2)


def test_site_collection_schedule_supports_12_and_24_hours(session_factory):
    with session_factory() as session:
        set_site_collection_hours(session, 12)
    scheduler = create_scheduler(session_factory)
    trigger = str(scheduler.get_job("collect-sites").trigger)
    assert "hour='8,20'" in trigger

    with session_factory() as session:
        set_site_collection_hours(session, 24)
    scheduler = create_scheduler(session_factory)
    trigger = str(scheduler.get_job("collect-sites").trigger)
    assert "hour='8'" in trigger
