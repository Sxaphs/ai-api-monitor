from __future__ import annotations

import json
import logging
from datetime import datetime
from urllib.parse import quote
from email.utils import parsedate_to_datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings, settings
from ..models import Snapshot, Target
from ..services.changes import add_change
from ..services.signals import classify_tweet, is_original_tweet
from ..time_utils import beijing_day_start_utc, ensure_utc, utc_now
from .http import Fetcher
from .sites import CollectStats

logger = logging.getLogger(__name__)


class TwitterClient:
    def __init__(self, fetcher: Fetcher, config: Settings = settings) -> None:
        self.fetcher = fetcher
        self.config = config

    def timeline(self, handle: str) -> list[dict[str, Any]]:
        if not self.config.rapidapi_key:
            raise RuntimeError("未配置 RAPIDAPI_KEY")
        host = self.config.rapidapi_host
        screenname = quote(handle.lstrip("@"), safe="")
        payload = self.fetcher.fetch_json(
            f"https://{host}/timeline.php?screenname={screenname}",
            headers={"X-RapidAPI-Key": self.config.rapidapi_key, "X-RapidAPI-Host": host},
        )
        timeline = payload.get("timeline", [])
        if not isinstance(timeline, list):
            raise RuntimeError("twitter-api45 返回的 timeline 不是数组")
        return [item for item in timeline if isinstance(item, dict)]


def tweet_id(tweet: dict[str, Any]) -> str | None:
    value = tweet.get("tweet_id") or tweet.get("id_str") or tweet.get("id")
    return str(value) if value not in (None, "") else None


def tweet_text(tweet: dict[str, Any]) -> str:
    return str(tweet.get("text") or tweet.get("full_text") or "").strip()


def tweet_time(tweet: dict[str, Any]) -> datetime | None:
    value = tweet.get("created_at") or tweet.get("created_at_timestamp")
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=utc_now().tzinfo)
    if not value:
        return None
    text = str(value)
    try:
        return ensure_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        pass
    try:
        # X 原生格式，例如 Thu Nov 12 02:00:00 +0000 2026。
        return ensure_utc(datetime.strptime(text, "%a %b %d %H:%M:%S %z %Y"))
    except ValueError:
        pass
    try:
        return ensure_utc(parsedate_to_datetime(text))
    except (TypeError, ValueError):
        return None


class TwitterCollector:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        client: TwitterClient | None = None,
        config: Settings = settings,
    ) -> None:
        self.session_factory = session_factory
        self.client = client or TwitterClient(Fetcher(config), config)

    def collect_all(self, groups: tuple[str, ...] = ("A", "D")) -> dict[str, int]:
        stats = CollectStats()
        with self.session_factory() as session:
            target_ids = session.scalars(
                select(Target.id)
                .where(Target.enabled.is_(True), Target.group_code.in_(groups))
                .order_by(Target.id)
            ).all()
        for target_id in target_ids:
            with self.session_factory() as session:
                target = session.get(Target, target_id)
                if target is None or not target.enabled:
                    continue
                if not target.x_handle:
                    stats.skipped += 1
                    continue
                try:
                    stats.changes += self.collect_target(session, target)
                    stats.success += 1
                except Exception as exc:
                    session.rollback()
                    logger.exception("X 采集失败 target=%s handle=%s", target.name, target.x_handle)
                    session.add(Snapshot(
                        target_id=target.id,
                        kind="x_timeline",
                        source_url=f"https://x.com/{target.x_handle.lstrip('@')}",
                        collected_at=utc_now(),
                        status="failed",
                        parse_ok=False,
                        error=f"{type(exc).__name__}: {exc}"[:8000],
                    ))
                    session.commit()
                    stats.failed += 1
        result = stats.as_dict()
        result["tweets"] = result["changes"]
        return result

    def collect_target(self, session: Session, target: Target) -> int:
        if target.x_since is None:
            target.x_since = utc_now()
            session.flush()
        since = beijing_day_start_utc(target.x_since)
        tweets = self.client.timeline(target.x_handle or "")
        created = 0
        for tweet in tweets:
            external_id = tweet_id(tweet)
            text = tweet_text(tweet)
            if not external_id or not text or not is_original_tweet(tweet):
                stats_skip = True
                logger.debug("跳过非原创或无 ID 推文 target=%s", target.name)
                continue
            published_at = tweet_time(tweet)
            if published_at is None or published_at < since:
                continue
            exists = session.scalars(
                select(Snapshot.id).where(
                    Snapshot.target_id == target.id,
                    Snapshot.kind == "x_post",
                    Snapshot.external_id == external_id,
                )
            ).first()
            if exists is not None:
                continue
            created += self.process_tweet(session, target, tweet)
        session.commit()
        return created

    def process_tweet(
        self, session: Session, target: Target, tweet: dict[str, Any]
    ) -> int:
        """处理一条已确认在时间窗内的原创推文；测试也直接复用此入口。"""
        external_id = tweet_id(tweet)
        text = tweet_text(tweet)
        if not external_id or not text:
            return 0
        exists = session.scalars(
            select(Snapshot.id).where(
                Snapshot.target_id == target.id,
                Snapshot.kind == "x_post",
                Snapshot.external_id == external_id,
            )
        ).first()
        if exists is not None:
            return 0
        handle = (target.x_handle or "x").lstrip("@")
        source_url = str(tweet.get("url") or f"https://x.com/{handle}/status/{external_id}")
        published_at = tweet_time(tweet)
        snapshot = Snapshot(
            target_id=target.id,
            kind="x_post",
            source_url=source_url,
            external_id=external_id,
            raw_content=json.dumps(tweet, ensure_ascii=False),
            extracted_json={
                "tweet_id": external_id,
                "text": text,
                "created_at": published_at.isoformat() if published_at else None,
                "handle": target.x_handle,
            },
            collected_at=utc_now(),
            status="success",
            parse_ok=True,
        )
        session.add(snapshot)
        session.flush()
        signal = classify_tweet(text, target.group_code)
        summary = f"@{handle}: {text}"
        add_change(
            session,
            target=target,
            snapshot=snapshot,
            change_type=signal.change_type,
            entity_key=f"tweet:{external_id}",
            summary=summary,
            signal_type=signal.signal_type,
            severity=signal.severity,
            old_value=signal.old_value,
            new_value=signal.new_value,
            change_percent=signal.change_percent,
            source_url=source_url,
            extra_json={
                "source_kind": "x",
                "warning_keywords": signal.matched_warnings,
                "tweet_created_at": published_at.isoformat() if published_at else None,
            },
        )
        return 1
