from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Change, Target
from ..time_utils import parse_datetime
from .changes import add_change

DEFAULT_START = parse_datetime("2026-10-12")
DEFAULT_END = parse_datetime("2026-11-13")
CHANGE_TYPES = {"涨价", "降价", "价格变动", "新模型", "模型下线", "新功能", "营销活动", "口径变化", "X公告"}
VERIFY_STATUSES = {"pending", "confirmed", "false_alarm"}
SIGNAL_TYPES = {"confirmed", "warning"}
SEVERITIES = {"high", "medium", "low"}
EVIDENCE_LEVELS = {"official", "secondary_pending"}
REQUIRED_FIELDS = {
    "target_name", "event_at", "change_type", "summary", "model",
    "old_value", "new_value", "change_percent", "source_url", "severity",
    "verify_status", "signal_type", "evidence_level", "source_title",
    "published_at", "event_key",
}


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _validate_item(
    item: Any,
    *,
    file_name: str,
    index: int,
    target_names: set[str],
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    location = f"{file_name} 第 {index} 条"
    if not isinstance(item, dict):
        raise ValueError(f"{location}：必须是 JSON 对象")
    missing = REQUIRED_FIELDS - item.keys()
    if missing:
        raise ValueError(f"{location}：缺少字段 {sorted(missing)}")
    if item["target_name"] not in target_names:
        raise ValueError(f"{location}：未知监测对象 {item['target_name']!r}")
    if item["change_type"] not in CHANGE_TYPES:
        raise ValueError(f"{location}：不支持的变动类型 {item['change_type']!r}")
    if item["verify_status"] not in VERIFY_STATUSES or item["signal_type"] not in SIGNAL_TYPES:
        raise ValueError(f"{location}：verify_status 或 signal_type 非法")
    if item["severity"] not in SEVERITIES or item["evidence_level"] not in EVIDENCE_LEVELS:
        raise ValueError(f"{location}：severity 或 evidence_level 非法")
    if not isinstance(item["summary"], str) or not item["summary"].strip():
        raise ValueError(f"{location}：summary 不能为空")
    if not isinstance(item["event_key"], str) or not item["event_key"].strip():
        raise ValueError(f"{location}：event_key 不能为空")
    parsed_url = urlparse(str(item["source_url"]))
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ValueError(f"{location}：source_url 必须是有效 http(s) URL")
    try:
        event_at = parse_datetime(str(item["event_at"]))
    except ValueError as exc:
        raise ValueError(f"{location}：event_at 日期非法") from exc
    if not start <= event_at < end:
        raise ValueError(f"{location}：event_at 不在导入窗口内")
    if item["change_percent"] is not None and _decimal(item["change_percent"]) is None:
        raise ValueError(f"{location}：change_percent 必须是数值或 null")
    for field in ("model", "old_value", "new_value", "source_title", "published_at"):
        if item[field] is not None and not isinstance(item[field], (str, int, float)):
            raise ValueError(f"{location}：{field} 必须是字符串、数值或 null")
    normalized = dict(item)
    normalized["event_at"] = event_at
    normalized["change_percent"] = _decimal(item["change_percent"])
    return normalized


def _calculated_percent(item: dict[str, Any]) -> Decimal | None:
    if item["change_percent"] is not None:
        return item["change_percent"]
    old_price = _decimal(item["old_value"])
    new_price = _decimal(item["new_value"])
    if old_price is None or new_price is None or old_price == 0:
        return None
    return ((new_price - old_price) / old_price * 100).quantize(Decimal("0.0001"))


def import_history(
    session: Session,
    directory: str | Path,
    *,
    start: datetime = DEFAULT_START,
    end: datetime = DEFAULT_END,
) -> dict[str, Any]:
    source_dir = Path(directory)
    if not source_dir.is_dir():
        raise ValueError(f"导入目录不存在：{source_dir}")
    files = sorted(source_dir.glob("*.json"))
    if not files:
        raise ValueError(f"导入目录没有 JSON 文件：{source_dir}")

    targets = {target.name: target for target in session.scalars(select(Target)).all()}
    pending_items: list[tuple[Path, dict[str, Any]]] = []
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"无法读取 {path.name}：{exc}") from exc
        if not isinstance(data, list):
            raise ValueError(f"{path.name}：顶层必须是 JSON 数组")
        for index, item in enumerate(data, start=1):
            pending_items.append(
                (
                    path,
                    _validate_item(
                        item,
                        file_name=path.name,
                        index=index,
                        target_names=set(targets),
                        start=start,
                        end=end,
                    ),
                )
            )

    added = 0
    skipped = 0
    per_target: dict[str, dict[str, int]] = defaultdict(lambda: {"added": 0, "skipped": 0})
    for path, item in sorted(pending_items, key=lambda row: (row[1]["event_at"], row[0].name, row[1]["event_key"])):
        target = targets[item["target_name"]]
        digest = hashlib.sha256(f"{target.name}:{item['event_key']}".encode("utf-8")).hexdigest()
        entity_key = f"import:{digest}"
        exists = session.scalars(
            select(Change.id).where(
                Change.target_id == target.id,
                Change.data_source == "import",
                Change.entity_key == entity_key,
            )
        ).first()
        if exists is not None:
            skipped += 1
            per_target[target.name]["skipped"] += 1
            continue
        add_change(
            session,
            target=target,
            snapshot=None,
            change_type=item["change_type"],
            entity_key=entity_key,
            summary=item["summary"].strip(),
            old_value=None if item["old_value"] is None else str(item["old_value"]),
            new_value=None if item["new_value"] is None else str(item["new_value"]),
            change_percent=_calculated_percent(item),
            signal_type=item["signal_type"],
            severity=item["severity"],
            verify_status=item["verify_status"],
            source_url=str(item["source_url"]),
            data_source="import",
            detected_at=item["event_at"],
            extra_json={
                "model": None if item["model"] is None else str(item["model"]),
                "source_kind": "history",
                "source_title": item["source_title"],
                "published_at": item["published_at"],
                "evidence_level": item["evidence_level"],
                "research_file": path.name,
                "event_key": item["event_key"],
            },
        )
        added += 1
        per_target[target.name]["added"] += 1
    session.commit()
    return {
        "added": added,
        "skipped": skipped,
        "files": len(files),
        "targets": dict(sorted(per_target.items())),
    }
