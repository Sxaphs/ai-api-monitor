from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Change, Report, Target
from ..time_utils import BEIJING, ensure_utc, utc_now

SECTION_TITLES = (
    "【本期概览】",
    "【价格变动汇总】",
    "【新模型与下线】",
    "【功能与营销活动】",
    "【预警回顾】",
)
PRICE_TYPES = {"价格变动", "涨价", "降价"}
MODEL_TYPES = {"新模型", "模型下线"}
ACTIVITY_TYPES = {"新功能", "营销活动", "口径变化", "X公告"}


def recent_report_window(days: int, now: datetime | None = None) -> tuple[datetime, datetime]:
    if days not in {7, 30}:
        raise ValueError("预设报告仅支持近 7 天或近 30 天")
    end = ensure_utc(now or utc_now())
    return end - timedelta(days=days), end


def _local(value: datetime, *, date_only: bool = False) -> str:
    local = ensure_utc(value).astimezone(BEIJING)
    return local.strftime("%Y-%m-%d" if date_only else "%Y-%m-%d %H:%M")


def _percent(value: Decimal | None) -> str:
    if value is None:
        return "未披露/无法计算"
    return f"{value.quantize(Decimal('0.01')):+f}%"


def _link(label: str, url: str | None) -> str:
    return f"[{label}]({url})" if url else f"{label}未提供"


def _review_suffix(change: Change) -> str:
    return "（待人工核对）" if change.verify_status == "pending" else ""


def _inline_text(value: object) -> str:
    """推文等多行文本进入 Markdown 列表前统一折叠为单行。"""
    return " ".join(str(value).split())


def _cell(value: str | None) -> str:
    if value is None or value == "":
        return "未披露"
    return _inline_text(value).replace("|", "\\|")


def _valid_landing(warning: Change, end: datetime) -> Change | None:
    confirmed = warning.related
    if (
        warning.verify_status != "confirmed"
        or confirmed is None
        or confirmed.signal_type != "confirmed"
        or confirmed.verify_status == "false_alarm"
    ):
        return None
    confirmed_at = ensure_utc(confirmed.detected_at)
    if not ensure_utc(warning.detected_at) <= confirmed_at < end:
        return None
    return confirmed


logger = logging.getLogger(__name__)


class ReportService:
    def __init__(self, session: Session, summarizer=None) -> None:
        self.session = session
        self.summarizer = summarizer

    def generate(
        self,
        start: datetime,
        end: datetime,
        *,
        title: str | None = None,
        report_kind: str = "custom",
        persist: bool = True,
    ) -> Report:
        start, end = ensure_utc(start), ensure_utc(end)
        if start >= end:
            raise ValueError("报告开始时间必须早于结束时间")
        rows = self.session.execute(
            select(Change, Target)
            .join(Target, Change.target_id == Target.id)
            .where(Change.detected_at >= start, Change.detected_at < end)
            .order_by(Change.detected_at.desc(), Change.id.desc())
        ).all()
        visible = [(change, target) for change, target in rows if change.verify_status != "false_alarm"]
        confirmed_rows = [
            (change, target) for change, target in visible if change.signal_type == "confirmed"
        ]
        warnings = [(change, target) for change, target in rows if change.signal_type == "warning"]

        landed: list[tuple[Change, Target, Change]] = []
        false_alarm_count = 0
        expired_count = 0
        observing_count = 0
        for warning, target in warnings:
            if warning.verify_status == "false_alarm":
                false_alarm_count += 1
                continue
            confirmed = _valid_landing(warning, end)
            if confirmed is not None:
                landed.append((warning, target, confirmed))
            elif ensure_utc(warning.detected_at) < end - timedelta(days=14):
                expired_count += 1
            else:
                observing_count += 1

        title = title or f"AI API 中转站竞品监测报告（{_local(start, date_only=True)} 至 {_local(end, date_only=True)}）"
        lines = [
            f"# {title}",
            "",
            f"统计窗口：{_local(start)} 至 {_local(end)}（北京时间，结束时间不含）",
            "",
            f"## {SECTION_TITLES[0]}",
            "",
            f"- 变动总数：{len(visible)} 条",
            f"- 涉及站点数：{len({target.id for _, target in visible})} 个",
        ]
        if warnings:
            rate = (Decimal(len(landed)) * 100 / Decimal(len(warnings))).quantize(Decimal("0.01"))
            lines.append(f"- 预警落地率：{rate}%（{len(landed)}/{len(warnings)}）")
        else:
            lines.append("- 预警落地率：本期无预警")

        lines.extend(["", f"## {SECTION_TITLES[1]}", ""])
        price_missing = sorted({target.name for _, target in visible if target.price_data_missing})
        for target_name in price_missing:
            lines.append(f"> {target_name}：该站价格表需登录，本期暂缺，仅监测口径变化。")
        if price_missing:
            lines.append("")
        price_rows = sorted(
            [(change, target) for change, target in confirmed_rows if change.change_type in PRICE_TYPES],
            key=lambda row: (row[1].name, ensure_utc(row[0].detected_at), row[0].id),
        )
        if price_rows:
            lines.extend([
                "| 站点 | 模型 | 旧价 | 新价 | 幅度 | 日期 |",
                "|---|---|---|---|---:|---|",
            ])
            for change, target in price_rows:
                model = str((change.extra_json or {}).get("model") or "—")
                date = _local(change.detected_at, date_only=True)
                if change.source_url:
                    date = f"[{date}]({change.source_url})"
                lines.append(
                    f"| {_cell(target.name)} | {_cell(model)} | {_cell(change.old_value)} | "
                    f"{_cell(change.new_value)} | {_percent(change.change_percent)} | {date} |"
                )
        else:
            lines.append("本期无")

        lines.extend(["", f"## {SECTION_TITLES[2]}", ""])
        model_groups: dict[str, list[Change]] = defaultdict(list)
        for change, target in confirmed_rows:
            if change.change_type in MODEL_TYPES:
                model_groups[target.name].append(change)
        if model_groups:
            for target_name in sorted(model_groups):
                lines.append(f"### {target_name}")
                lines.append("")
                for change in sorted(model_groups[target_name], key=lambda item: ensure_utc(item.detected_at)):
                    lines.append(
                        f"- **{change.change_type}**（{_local(change.detected_at, date_only=True)}）："
                        f"{_inline_text(change.summary)}{_review_suffix(change)}；{_link('原文', change.source_url)}"
                    )
                lines.append("")
        else:
            lines.append("本期无")

        lines.extend(["", f"## {SECTION_TITLES[3]}", ""])
        activity_rows = sorted(
            [(change, target) for change, target in confirmed_rows if change.change_type in ACTIVITY_TYPES],
            key=lambda row: (row[1].name, ensure_utc(row[0].detected_at), row[0].id),
        )
        if activity_rows:
            for change, target in activity_rows:
                lines.append(
                    f"- **{target.name}｜{change.change_type}**"
                    f"（{_local(change.detected_at, date_only=True)}）：{_inline_text(change.summary)}"
                    f"{_review_suffix(change)}；{_link('原文', change.source_url)}"
                )
        else:
            lines.append("本期无")

        lines.extend(["", f"## {SECTION_TITLES[4]}", ""])
        if warnings:
            lines.append(
                f"- 落地 {len(landed)} 条；误报 {false_alarm_count} 条；"
                f"超时未落地 {expired_count} 条；仍在观察期 {observing_count} 条。"
            )
            if landed:
                lines.extend(["", "### 已落地预警", ""])
                for warning, target, confirmed_change in sorted(
                    landed, key=lambda row: ensure_utc(row[0].detected_at)
                ):
                    lines.append(
                        f"- **{target.name}**：预警「{_inline_text(warning.summary)}」→ "
                        f"落地「{_inline_text(confirmed_change.summary)}」；"
                        f"{_link('预警原文', warning.source_url)} / "
                        f"{_link('落地原文', confirmed_change.source_url)}"
                    )
            else:
                lines.extend(["", "本期无已落地预警"])
        else:
            lines.append("本期无")

        if self.summarizer is not None:
            factual_content = "\n".join(lines).rstrip() + "\n"
            try:
                summary = self.summarizer.summarize(factual_content)
            except Exception:
                logger.exception("模型总结生成失败，继续保存原始统计报告")
                summary = "模型总结生成失败，请检查 LLM 配置与网络；下方为原始统计数据。"
            lines = lines[:4] + ["## 【模型总结】", "", summary, ""] + lines[4:]

        content = "\n".join(lines).rstrip() + "\n"
        report = Report(
            title=title,
            report_kind=report_kind,
            period_start=start,
            period_end=end,
            content=content,
        )
        if persist:
            self.session.add(report)
            self.session.commit()
        return report
