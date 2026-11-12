from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Change, Snapshot, Target
from ..time_utils import ensure_utc, utc_now

PRICE_TYPES = {"价格变动", "涨价", "降价", "口径变化"}
FEATURE_TYPES = {"新模型", "新功能"}


def add_change(
    session: Session,
    *,
    target: Target,
    snapshot: Snapshot | None,
    change_type: str,
    summary: str,
    signal_type: str = "confirmed",
    severity: str = "medium",
    verify_status: str = "pending",
    entity_key: str | None = None,
    old_value: str | None = None,
    new_value: str | None = None,
    change_percent: Decimal | None = None,
    source_url: str | None = None,
    data_source: str | None = None,
    detected_at=None,
    extra_json: dict[str, Any] | None = None,
) -> Change:
    extra = dict(extra_json or {})
    if snapshot and snapshot.dual_match is False:
        extra.setdefault("dual_snapshot_id", snapshot.id)
        extra.setdefault("dual_difference_fields", snapshot.dual_difference_fields or [])
    change = Change(
        target_id=target.id,
        snapshot_id=snapshot.id if snapshot else None,
        change_type=change_type,
        entity_key=entity_key or (snapshot.external_id if snapshot else None),
        summary=summary,
        old_value=old_value,
        new_value=new_value,
        change_percent=change_percent,
        signal_type=signal_type,
        severity=severity,
        verify_status=verify_status,
        source_url=source_url or (snapshot.source_url if snapshot else None),
        data_source=data_source,
        detected_at=detected_at or utc_now(),
        verified_at=utc_now() if verify_status != "pending" else None,
        extra_json=extra or None,
    )
    session.add(change)
    session.flush()
    if signal_type == "confirmed":
        close_related_warning(session, change)
    return change


def _compatible(warning_type: str, confirmed_type: str) -> bool:
    if warning_type == confirmed_type:
        return True
    if warning_type in PRICE_TYPES and confirmed_type in PRICE_TYPES:
        return True
    return warning_type in FEATURE_TYPES and confirmed_type in FEATURE_TYPES


def close_related_warning(session: Session, confirmed: Change) -> Change | None:
    if confirmed.related_id is not None:
        return None
    detected_at = ensure_utc(confirmed.detected_at)
    candidates = session.scalars(
        select(Change)
        .where(
            Change.target_id == confirmed.target_id,
            Change.signal_type == "warning",
            Change.verify_status == "pending",
            Change.related_id.is_(None),
            Change.id != confirmed.id,
        )
        .order_by(Change.detected_at.desc(), Change.id.desc())
    ).all()
    for warning in candidates:
        warning_time = ensure_utc(warning.detected_at)
        if not detected_at - timedelta(days=14) <= warning_time <= detected_at:
            continue
        if not _compatible(warning.change_type, confirmed.change_type):
            continue
        warning.related_id = confirmed.id
        warning.verify_status = "confirmed"
        warning.verified_at = utc_now()
        confirmed.related_id = warning.id
        session.flush()
        return warning
    return None


def previous_baseline(
    session: Session, *, target_id: int, kind: str, current_snapshot_id: int
) -> Snapshot | None:
    return session.scalars(
        select(Snapshot)
        .where(
            Snapshot.target_id == target_id,
            Snapshot.kind == kind,
            Snapshot.status == "success",
            Snapshot.parse_ok.is_(True),
            Snapshot.id != current_snapshot_id,
        )
        .order_by(Snapshot.collected_at.desc(), Snapshot.id.desc())
        .limit(1)
    ).first()


def _display_price(model: dict[str, Any], field: str) -> str:
    parts = [str(model.get(field))]
    if model.get("currency"):
        parts.append(str(model["currency"]))
    if model.get("price_unit"):
        parts.append(str(model["price_unit"]))
    return " ".join(parts)


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value is not None else None
    except (InvalidOperation, ValueError):
        return None


def _models_by_name(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw_models = data.get("models", {})
    items = raw_models.values() if isinstance(raw_models, dict) else raw_models
    models: dict[str, dict[str, Any]] = {}
    if not isinstance(items, (list, tuple, type({}.values()))):
        return models
    for item in items:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        model = dict(item)
        model["group"] = item.get("tier_or_group") or item.get("group")
        model["price_unit"] = item.get("unit") or item.get("price_unit")
        model.setdefault("multiplier", None)
        models[str(item["name"])] = model
    return models


def _page_value(data: dict[str, Any], field: str) -> Any:
    page_level = data.get("page_level")
    value = page_level.get(field) if isinstance(page_level, dict) else data.get(field)
    return None if value in (None, "", [], {}) else value


def diff_pricing(
    session: Session, *, target: Target, previous: Snapshot, current: Snapshot
) -> list[Change]:
    old_data = previous.extracted_json or {}
    new_data = current.extracted_json or {}
    old_models = _models_by_name(old_data)
    new_models = _models_by_name(new_data)
    base_verify = "pending" if new_data.get("validation_pending") or current.dual_match is False else "confirmed"
    created: list[Change] = []

    for name in sorted(new_models.keys() - old_models.keys()):
        created.append(add_change(
            session, target=target, snapshot=current, change_type="新模型",
            entity_key=f"pricing:{name}", summary=f"定价页新增模型 {name}",
            new_value=name, severity="medium", verify_status=base_verify,
            data_source="webpage",
        ))
    for name in sorted(old_models.keys() - new_models.keys()):
        created.append(add_change(
            session, target=target, snapshot=current, change_type="模型下线",
            entity_key=f"pricing:{name}", summary=f"定价页不再列出模型 {name}",
            old_value=name, severity="high", verify_status=base_verify,
            data_source="webpage",
        ))

    for name in sorted(old_models.keys() & new_models.keys()):
        old_model = old_models[name]
        new_model = new_models[name]
        for field, label in (("input_price", "输入价"), ("output_price", "输出价")):
            old_raw, new_raw = old_model.get(field), new_model.get(field)
            if old_raw == new_raw:
                continue
            old_price, new_price = _decimal(old_raw), _decimal(new_raw)
            entity_key = f"pricing:{name}:{field}"
            old_display = _display_price(old_model, field) if old_raw is not None else None
            new_display = _display_price(new_model, field) if new_raw is not None else None
            if old_price is None or new_price is None:
                change_type = "口径变化"
                percent = None
            else:
                change_type = "口径变化" if new_price == old_price else ("涨价" if new_price > old_price else "降价")
                percent = None if old_price == 0 else ((new_price - old_price) / old_price * 100).quantize(Decimal("0.0001"))
            needs_review = base_verify == "pending" or percent is None or abs(percent) > 50
            suffix = "，旧值为零，幅度不可计算" if old_price == 0 and new_price is not None else ""
            if percent is not None and abs(percent) > 50:
                suffix += "，幅度超过 50%，待人工核对"
            created.append(add_change(
                session, target=target, snapshot=current, change_type=change_type,
                entity_key=entity_key, summary=f"{name} {label}调整{suffix}",
                old_value=old_display, new_value=new_display, change_percent=percent,
                verify_status="pending" if needs_review else "confirmed",
                data_source="webpage",
                extra_json={"model": name, "price_field": field, "source_kind": "pricing"},
            ))

        for field in ("group", "multiplier", "currency", "price_unit"):
            if old_model.get(field) == new_model.get(field):
                continue
            created.append(add_change(
                session, target=target, snapshot=current, change_type="口径变化",
                entity_key=f"pricing:{name}:{field}",
                summary=f"{name} 的 {field} 口径变化",
                old_value=json.dumps(old_model.get(field), ensure_ascii=False),
                new_value=json.dumps(new_model.get(field), ensure_ascii=False),
                verify_status=base_verify, data_source="webpage",
                extra_json={"model": name, "metadata_field": field, "source_kind": "pricing"},
            ))

    for field, label in (
        ("billing_method", "计费方式"),
        ("exchange_rate", "汇率口径"),
        ("minimum_recharge", "最低充值"),
        ("discount_rules", "赠金/折扣规则"),
    ):
        old_value = _page_value(old_data, field)
        new_value = _page_value(new_data, field)
        if old_value == new_value:
            continue
        created.append(add_change(
            session, target=target, snapshot=current, change_type="口径变化",
            entity_key=f"pricing:site:{field}", summary=f"{label}变化",
            old_value=json.dumps(old_value, ensure_ascii=False),
            new_value=json.dumps(new_value, ensure_ascii=False),
            verify_status=base_verify, data_source="webpage",
            extra_json={"metadata_field": field, "source_kind": "pricing"},
        ))
    return created


def diff_models(
    session: Session, *, target: Target, previous: Snapshot, current: Snapshot
) -> list[Change]:
    old_models = set((previous.extracted_json or {}).get("models", []))
    new_models = set((current.extracted_json or {}).get("models", []))
    source = "webpage" if current.kind == "models_webpage" else "api"
    label = "公开页面" if source == "webpage" else "模型接口"
    verify_status = "pending" if (current.extracted_json or {}).get("validation_pending") else "confirmed"
    created: list[Change] = []
    for name in sorted(new_models - old_models):
        created.append(add_change(
            session, target=target, snapshot=current, change_type="新模型",
            entity_key=f"models:{name}", summary=f"{label}新增 {name}", new_value=name,
            verify_status=verify_status, data_source=source,
            extra_json={"source_kind": "models", "data_source": source},
        ))
    for name in sorted(old_models - new_models):
        created.append(add_change(
            session, target=target, snapshot=current, change_type="模型下线",
            entity_key=f"models:{name}", summary=f"{label}移除 {name}", old_value=name,
            severity="high", verify_status=verify_status, data_source=source,
            extra_json={"source_kind": "models", "data_source": source},
        ))
    return created
