from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.orm import Session, sessionmaker

from .collectors.sites import SiteCollector
from .collectors.twitter import TwitterCollector
from .time_utils import BEIJING

logger = logging.getLogger(__name__)


def run_site_collection(session_factory: sessionmaker[Session]) -> None:
    logger.info("定时网站采集结果: %s", SiteCollector(session_factory).collect_all())


def run_x_collection(session_factory: sessionmaker[Session]) -> None:
    logger.info("定时 X 采集结果: %s", TwitterCollector(session_factory).collect_all())


def create_scheduler(session_factory: sessionmaker[Session]) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(
        timezone=BEIJING,
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600},
    )
    scheduler.add_job(
        run_site_collection,
        IntervalTrigger(hours=6, timezone=BEIJING),
        args=[session_factory],
        id="collect-sites-6h",
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
