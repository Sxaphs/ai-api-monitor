from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import Change, Target
from app.services.history_import import import_history


def make_item(**overrides):
    item = {
        "target_name": "OpenRouter",
        "event_at": "2026-11-01",
        "change_type": "降价",
        "summary": "测试模型输入价从 100 调整为 80",
        "model": "test-model 输入价",
        "old_value": "100",
        "new_value": "80",
        "change_percent": None,
        "source_url": "https://openrouter.ai/blog/test-change",
        "severity": "medium",
        "verify_status": "confirmed",
        "signal_type": "confirmed",
        "evidence_level": "official",
        "source_title": "Test change",
        "published_at": "2026-11-01",
        "event_key": "openrouter-test-price-20261101",
    }
    item.update(overrides)
    return item


def write_items(tmp_path, items):
    directory = tmp_path / "history"
    directory.mkdir()
    (directory / "test.json").write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return directory


def test_history_import_validates_calculates_and_is_idempotent(session, tmp_path):
    target = Target(name="OpenRouter", group_code="A")
    session.add(target)
    session.commit()
    directory = write_items(tmp_path, [make_item()])

    first = import_history(session, directory)
    assert first["added"] == 1 and first["skipped"] == 0
    change = session.scalars(select(Change)).one()
    assert change.target_id == target.id
    assert change.snapshot_id is None
    assert change.data_source == "import"
    assert change.entity_key.startswith("import:")
    assert change.detected_at.replace(tzinfo=timezone.utc) == datetime(2026, 10, 31, 16, tzinfo=timezone.utc)
    assert change.change_percent == Decimal("-20.0000")
    assert change.extra_json["research_file"] == "test.json"
    assert change.extra_json["source_title"] == "Test change"

    second = import_history(session, directory)
    assert second["added"] == 0 and second["skipped"] == 1
    assert session.scalars(select(Change)).all() == [change]


def test_history_import_rejects_invalid_source_without_inserting(session, tmp_path):
    session.add(Target(name="OpenRouter", group_code="A"))
    session.commit()
    directory = write_items(tmp_path, [make_item(source_url="not-a-url")])
    with pytest.raises(ValueError, match="source_url"):
        import_history(session, directory)
    assert session.scalars(select(Change)).all() == []
