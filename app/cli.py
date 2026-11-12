from __future__ import annotations

import argparse
import json
import logging

from .collectors.sites import SiteCollector
from .collectors.twitter import TwitterCollector
from .config import settings
from .db import SessionLocal, init_db
from .seed import seed_targets
from .services.history_import import import_history
from .services.migration import apply_migration, migration_report
from .services.reports import ReportService, recent_report_window
from .time_utils import BEIJING, parse_datetime


def _bootstrap() -> None:
    init_db()
    with SessionLocal() as session:
        seed_targets(session)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AI API 竞品监测系统命令行")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="建表并幂等写入种子数据")
    subparsers.add_parser("collect-sites", help="手动采集 A/B 组定价页和模型列表")
    subparsers.add_parser("collect-x", help="手动采集 A/D 组已配置 X 账号")
    subparsers.add_parser("collect", help="手动执行网站与 X 全量采集")
    migration = subparsers.add_parser("migration-status", help="查看双跑达标率；--apply 执行达标切换")
    migration.add_argument("--apply", action="store_true", help="将达到 95%% 且到评估期的站点切为 llm")
    report = subparsers.add_parser("report", help="手动生成近 7/30 天或自定义 Markdown 报告")
    report.add_argument("--days", type=int, choices=(7, 30), help="生成近 7 天周报或近 30 天月报")
    report.add_argument("--start", help="自定义 ISO 开始时间；naive 时间按北京时间解释")
    report.add_argument("--end", help="自定义 ISO 结束时间（不含）；naive 时间按北京时间解释")
    history = subparsers.add_parser("import-history", help="从结构化 JSON 目录幂等导入历史更新")
    history.add_argument("directory", help="包含一个或多个 JSON 数组文件的目录")
    return parser


def main() -> None:
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = build_parser().parse_args()
    _bootstrap()
    if args.command == "init-db":
        print("数据库和四组种子数据已就绪")
        return
    if args.command == "migration-status":
        with SessionLocal() as session:
            switched = apply_migration(session) if args.apply else []
            statuses = migration_report(session)
            print(json.dumps({"switched": switched, "targets": statuses}, ensure_ascii=False, indent=2))
        return
    if args.command == "import-history":
        with SessionLocal() as session:
            result = import_history(session, args.directory)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command in {"collect-sites", "collect"}:
        print(json.dumps({"sites": SiteCollector(SessionLocal).collect_all()}, ensure_ascii=False))
    if args.command in {"collect-x", "collect"}:
        print(json.dumps({"x": TwitterCollector(SessionLocal).collect_all()}, ensure_ascii=False))
    if args.command == "report":
        if bool(args.start) != bool(args.end):
            raise SystemExit("--start 和 --end 必须同时提供")
        if args.days and (args.start or args.end):
            raise SystemExit("--days 不能与 --start/--end 同时使用")
        if not args.days and not args.start:
            raise SystemExit("请指定 --days 7|30，或同时提供 --start 和 --end")
        if args.days:
            start, end = recent_report_window(args.days)
            title = f"AI API 中转站竞品监测{'周报' if args.days == 7 else '月报'}（近 {args.days} 天）"
            report_kind = "weekly" if args.days == 7 else "monthly"
        else:
            start, end = parse_datetime(args.start), parse_datetime(args.end)
            title = (
                f"AI API 中转站竞品监测自定义报告"
                f"（{start.astimezone(BEIJING):%Y-%m-%d %H:%M} 至 {end.astimezone(BEIJING):%Y-%m-%d %H:%M}）"
            )
            report_kind = "custom"
        with SessionLocal() as session:
            report = ReportService(session).generate(
                start, end, title=title, report_kind=report_kind
            )
            print(report.content, end="")


if __name__ == "__main__":
    main()
