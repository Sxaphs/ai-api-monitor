from __future__ import annotations

import json
import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Change, Snapshot, Target
from ..time_utils import BEIJING, ensure_utc, utc_now
from .changes import add_change

SHARED_PAGE_FIELDS = ("exchange_rate", "minimum_recharge", "discount_rules")
MODEL_FIELDS = ("input_price", "output_price", "currency", "unit", "tier_or_group")


def _decimal_string(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        return format(Decimal(str(value)).normalize(), "f")
    except (InvalidOperation, ValueError):
        return re.sub(r"\s+", " ", str(value)).strip()


def _unit(value: Any) -> str | None:
    if value in (None, ""):
        return None
    lower = str(value).strip().lower().replace(" ", "")
    if "百万" in lower or "per_1m" in lower or "/m" in lower or "million" in lower:
        return "per_1m_tokens"
    if "每千" in lower or "per_1k" in lower or "/k" in lower:
        return "per_1k"
    if "每次" in lower or "per_request" in lower or "per request" in lower:
        return "per_request"
    return lower


def _text(value: Any) -> str | None:
    if value in (None, "", [], {}):
        return None
    if isinstance(value, list):
        return "\n".join(sorted(re.sub(r"\s+", " ", str(item)).strip() for item in value))
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return re.sub(r"\s+", " ", str(value)).strip()


def canonical_extraction(data: dict[str, Any]) -> dict[str, Any]:
    raw_models = data.get("models", {})
    items = list(raw_models.values()) if isinstance(raw_models, dict) else raw_models
    models: dict[str, dict[str, Any]] = {}
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            name = re.sub(r"\s+", " ", str(item["name"])).strip()
            models[name] = {
                "input_price": _decimal_string(item.get("input_price")),
                "output_price": _decimal_string(item.get("output_price")),
                "currency": _text(item.get("currency")).upper() if _text(item.get("currency")) else None,
                "unit": _unit(item.get("unit") or item.get("price_unit")),
                "tier_or_group": _text(
                    item.get("tier_or_group") or item.get("group") or item.get("multiplier")
                ),
            }
    page_level = data.get("page_level") if isinstance(data.get("page_level"), dict) else data
    return {
        "models": models,
        "page_level": {field: _text(page_level.get(field)) for field in SHARED_PAGE_FIELDS},
    }


def compare_extractions(parser: dict[str, Any], llm: dict[str, Any]) -> dict[str, Any]:
    parser_data = canonical_extraction(parser)
    llm_data = canonical_extraction(llm)
    differences: list[dict[str, Any]] = []

    def add(field: str, parser_value: Any, llm_value: Any) -> None:
        if parser_value != llm_value:
            differences.append({"field": field, "parser": parser_value, "llm": llm_value})

    for name in sorted(parser_data["models"].keys() | llm_data["models"].keys()):
        parser_model = parser_data["models"].get(name)
        llm_model = llm_data["models"].get(name)
        if parser_model is None or llm_model is None:
            add(f"models[{name}]", parser_model is not None, llm_model is not None)
            continue
        for field in MODEL_FIELDS:
            add(f"models[{name}].{field}", parser_model[field], llm_model[field])
    for field in SHARED_PAGE_FIELDS:
        add(f"page_level.{field}", parser_data["page_level"][field], llm_data["page_level"][field])
    return {
        "match": not differences,
        "differences": differences,
        "parser": parser_data,
        "llm": llm_data,
    }


def record_dual_mismatch(
    session: Session, *, target: Target, snapshot: Snapshot, comparison: dict[str, Any]
) -> Change:
    differences = comparison["differences"]
    fields = "、".join(item["field"] for item in differences[:12])
    if len(differences) > 12:
        fields += f" 等 {len(differences)} 项"
    return add_change(
        session,
        target=target,
        snapshot=snapshot,
        change_type="提取分歧",
        entity_key=f"dual:{snapshot.id}",
        summary=f"双跑提取不一致：{fields}",
        signal_type="warning",
        severity="low",
        verify_status="pending",
        old_value=json.dumps({item["field"]: item["parser"] for item in differences}, ensure_ascii=False),
        new_value=json.dumps({item["field"]: item["llm"] for item in differences}, ensure_ascii=False),
        extra_json={
            "source_kind": "dual",
            "dual_snapshot_id": snapshot.id,
            "dual_difference_fields": differences,
        },
    )


def target_migration_status(
    session: Session, target: Target, *, now=None
) -> dict[str, Any]:
    now = ensure_utc(now or utc_now())
    samples = session.scalars(
        select(Snapshot).where(
            Snapshot.target_id == target.id,
            Snapshot.kind == "pricing",
            Snapshot.dual_successful.is_(True),
        )
    ).all()
    attempts = len(samples)
    matches = sum(sample.dual_match is True for sample in samples)
    manual = sum(sample.dual_match is False and sample.dual_llm_confirmed is True for sample in samples)
    accepted = matches + manual
    rate = accepted / attempts if attempts else 0.0
    started = ensure_utc(target.dual_started_at) if target.dual_started_at else None
    old_enough = started is not None and now - started >= timedelta(days=7)
    eligible = attempts >= 20 or old_enough
    ready = eligible and attempts > 0 and rate >= 0.95
    pending_cases = session.scalars(
        select(Snapshot)
        .where(
            Snapshot.target_id == target.id,
            Snapshot.kind == "pricing",
            Snapshot.dual_match.is_(False),
            Snapshot.dual_llm_confirmed.is_not(True),
        )
        .order_by(Snapshot.collected_at.desc())
        .limit(20)
    ).all()
    return {
        "target_id": target.id,
        "name": target.name,
        "extraction_method": target.extraction_method,
        "dual_started_at": started.isoformat() if started else None,
        "successful_dual_runs": attempts,
        "matched": matches,
        "manual_llm_confirmed": manual,
        "accepted": accepted,
        "acceptance_rate": round(rate, 4),
        "evaluation_reached": eligible,
        "ready_for_llm": ready,
        "pending_cases": [
            {"snapshot_id": item.id, "differences": item.dual_difference_fields or []}
            for item in pending_cases
        ],
    }


def migration_report(session: Session, *, now=None) -> list[dict[str, Any]]:
    targets = session.scalars(
        select(Target).where(Target.pricing_url.is_not(None)).order_by(Target.id)
    ).all()
    statuses = [target_migration_status(session, target, now=now) for target in targets]
    # 普通/简单结构优先，OpenRouter 与 302.AI 明确最后。
    return sorted(
        statuses,
        key=lambda item: (item["name"] in {"OpenRouter", "302.AI"}, item["target_id"]),
    )


def apply_migration(session: Session, *, now=None) -> list[str]:
    switched: list[str] = []
    for status in migration_report(session, now=now):
        if not status["ready_for_llm"]:
            continue
        target = session.get(Target, status["target_id"])
        if target and target.extraction_method == "dual":
            target.extraction_method = "llm"
            target.updated_at = utc_now()
            switched.append(target.name)
    session.commit()
    return switched


def llm_usage_by_day(session: Session, *, days: int = 7, now=None) -> list[dict[str, Any]]:
    now = ensure_utc(now or utc_now())
    rows = session.execute(
        select(Snapshot, Target)
        .join(Target, Snapshot.target_id == Target.id)
        .where(Snapshot.collected_at >= now - timedelta(days=days), Snapshot.llm_calls > 0)
    ).all()
    totals: dict[tuple[str, str], dict[str, Any]] = {}
    for snapshot, target in rows:
        day = ensure_utc(snapshot.collected_at).astimezone(BEIJING).date().isoformat()
        key = (day, target.name)
        totals.setdefault(key, {"date": day, "target_name": target.name, "calls": 0, "estimated_tokens": 0})
        totals[key]["calls"] += snapshot.llm_calls
        totals[key]["estimated_tokens"] += snapshot.llm_estimated_tokens
    return sorted(totals.values(), key=lambda item: (item["date"], item["target_name"]), reverse=True)
