from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.orm import Session, sessionmaker

from .collectors.sites import SiteCollector
from .collectors.twitter import TwitterCollector
from .models import AppSetting
from .time_utils import BEIJING

logger = logging.getLogger(__name__)
SITE_COLLECTION_SETTING = "site_collection_hours"
COLLECTION_HOURS_OPTIONS = (6, 12, 24)


def get_site_collection_hours(session: Session) -> int:
    setting = session.get(AppSetting, SITE_COLLECTION_SETTING)
    try:
        hours = int(setting.value) if setting else 6
    except (TypeError, ValueError):
        return 6
    return hours if hours in COLLECTION_HOURS_OPTIONS else 6


def set_site_collection_hours(session: Session, hours: int) -> None:
    if hours not in COLLECTION_HOURS_OPTIONS:
        raise ValueError("网站采集频率仅支持 6、12 或 24 小时")
    setting = session.get(AppSetting, SITE_COLLECTION_SETTING)
    if setting is None:
        setting = AppSetting(key=SITE_COLLECTION_SETTING, value=str(hours))
        session.add(setting)
    else:
        setting.value = str(hours)
    session.commit()


def site_collection_times(hours: int) -> str:
    if hours not in COLLECTION_HOURS_OPTIONS:
        raise ValueError("网站采集频率仅支持 6、12 或 24 小时")
    times = sorted((8 + offset * hours) % 24 for offset in range(24 // hours))
    return " / ".join(f"{hour:02d}:00" for hour in times)


def site_collection_trigger(hours: int) -> CronTrigger:
    return CronTrigger(
        hour=",".join(str(int(value[:2])) for value in site_collection_times(hours).split(" / ")),
        minute=0,
        timezone=BEIJING,
    )


def run_site_collection(session_factory: sessionmaker[Session]) -> None:
    logger.info("定时网站采集结果: %s", SiteCollector(session_factory).collect_all())


def run_x_collection(session_factory: sessionmaker[Session]) -> None:
    logger.info("定时 X 采集结果: %s", TwitterCollector(session_factory).collect_all())


def create_scheduler(session_factory: sessionmaker[Session]) -> BackgroundScheduler:
    with session_factory() as session:
        site_hours = get_site_collection_hours(session)
    scheduler = BackgroundScheduler(
        timezone=BEIJING,
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600},
    )
    scheduler.add_job(
        run_site_collection,
        site_collection_trigger(site_hours),
        args=[session_factory],
        id="collect-sites",
        replace_existing=True,
    )
    scheduler.add_job(
        run_x_collection,
        IntervalTrigger(hours=2, timezone=BEIJING),
        args=[session_factory],
        id="collect-x-2h",
        replace_existing=True,
    )
    return scheduler
