from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.collectors.twitter import TwitterCollector
from app.models import Change, Snapshot, Target
from app.services.signals import classify_tweet

UTC = timezone.utc


class StubTwitterClient:
    def __init__(self, tweets):
        self.tweets = tweets
        self.calls = 0

    def timeline(self, handle):
        self.calls += 1
        return self.tweets


def make_target(group="A"):
    return Target(
        name=f"target-{group}", group_code=group, x_handle="@OpenRouterAI",
        x_since=datetime(2026, 11, 12, 1, tzinfo=UTC),
    )


def test_original_filter_dedup_and_no_history_backfill(session_factory, session):
    target = make_target()
    session.add(target)
    session.commit()
    tweets = [
        {"tweet_id": "new", "text": "New model now available", "created_at": "Thu Nov 12 02:00:00 +0000 2026"},
        {"tweet_id": "retweet", "text": "RT @someone New model launch", "created_at": "2026-11-12T02:01:00+00:00"},
        {"tweet_id": "reply", "text": "support this", "in_reply_to_status_id": "123", "created_at": "2026-11-12T02:02:00+00:00"},
        {"tweet_id": "old", "text": "New model launch", "created_at": "2026-11-10T02:00:00+00:00"},
        {"tweet_id": "new", "text": "New model now available", "created_at": "2026-11-12T02:00:00+00:00"},
    ]
    client = StubTwitterClient(tweets)
    collector = TwitterCollector(session_factory, client=client)
    first = collector.collect_all()
    second = collector.collect_all()
    assert first["tweets"] == 1
    assert second["tweets"] == 0
    assert client.calls == 2
    snapshots = session.scalars(select(Snapshot).where(Snapshot.kind == "x_post")).all()
    assert len(snapshots) == 1
    change = session.scalar(select(Change))
    assert change.change_type == "新模型"
    assert change.signal_type == "confirmed"
    assert change.source_url == "https://x.com/OpenRouterAI/status/new"


def test_coming_soon_and_upstream_classification(session):
    target = make_target()
    session.add(target)
    session.commit()
    collector = TwitterCollector(None, client=StubTwitterClient([]))
    created = collector.process_tweet(session, target, {
        "tweet_id": "warning", "text": "New model coming soon",
        "created_at": "2026-11-12T02:00:00+00:00",
    })
    session.commit()
    assert created == 1
    change = session.scalar(select(Change))
    assert change.signal_type == "warning"
    assert change.severity == "medium"
    assert change.verify_status == "pending"

    upstream = classify_tweet("Pricing update: $1 -> $2", "D")
    assert upstream.signal_type == "warning"
    assert upstream.severity == "high"
    assert upstream.old_value == "1"
    assert upstream.new_value == "2"
    assert float(upstream.change_percent) == 100.0
