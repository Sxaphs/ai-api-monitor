from __future__ import annotations

from datetime import datetime, time, timezone

from zoneinfo import ZoneInfo

UTC = timezone.utc
BEIJING = ZoneInfo("Asia/Shanghai")


def utc_now() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    """SQLite 读取时会丢时区；统一按 UTC 解释 naive 时间。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING)
    return parsed.astimezone(UTC)


def beijing_day_start_utc(value: datetime) -> datetime:
    local = ensure_utc(value).astimezone(BEIJING)
    return datetime.combine(local.date(), time.min, tzinfo=BEIJING).astimezone(UTC)
