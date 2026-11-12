from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.collectors.llm import crop_pricing_text
from app.models import Snapshot, Target
from app.seed import seed_targets
from app.services.migration import (
    apply_migration,
    compare_extractions,
    migration_report,
    target_migration_status,
)

UTC = timezone.utc


def test_dual_comparison_normalizes_price_format_and_unit():
    parser = {
        "models": {"demo": {"name": "demo", "input_price": "1.0", "output_price": "2.00", "currency": "usd", "price_unit": "per_1m_tokens", "group": "default"}},
        "exchange_rate": ["1 USD = 7.20 CNY"], "minimum_recharge": [], "discount_rules": [],
    }
    llm = {
        "models": [{"name": "demo", "input_price": "1.00", "output_price": "2.0", "currency": "USD", "unit": "每百万token", "tier_or_group": "default", "note": "parser 没有此字段"}],
        "page_level": {"exchange_rate": "1 USD = 7.20 CNY", "minimum_recharge": None, "discount_rules": None},
    }
    assert compare_extractions(parser, llm)["match"] is True
    llm["models"][0]["input_price"] = "1.10"
    comparison = compare_extractions(parser, llm)
    assert comparison["match"] is False
    assert comparison["differences"][0]["field"] == "models[demo].input_price"
    assert comparison["differences"][0]["parser"] == "1"
    assert comparison["differences"][0]["llm"] == "1.1"


def _target(name, started):
    return Target(name=name, group_code="A", pricing_url=f"https://{name}.test", extraction_method="dual", dual_started_at=started)


def _samples(session, target, total, matches, manual=0):
    session.add(target)
    session.flush()
    for index in range(total):
        matched = index < matches
        session.add(Snapshot(
            target_id=target.id, kind="pricing", status="success", parse_ok=True,
            dual_successful=True, dual_match=matched,
            dual_llm_confirmed=(not matched and index < matches + manual),
        ))


def test_95_percent_gate_and_evaluation_window(session):
    now = datetime(2026, 11, 12, tzinfo=UTC)
    exact = _target("exact-95", now - timedelta(days=1))
    low = _target("low-90", now - timedelta(days=8))
    early = _target("early", now - timedelta(days=1))
    old = _target("seven-days", now - timedelta(days=7))
    _samples(session, exact, 20, 18, manual=1)  # 18 一致 + 1 人工 = 19/20
    _samples(session, low, 20, 18)             # 90%，满 7 天也不切
    _samples(session, early, 1, 1)              # 100%，但未到评估期
    _samples(session, old, 2, 2)                # 满 7 天，100%
    session.commit()

    assert target_migration_status(session, exact, now=now)["ready_for_llm"] is True
    assert target_migration_status(session, exact, now=now)["acceptance_rate"] == 0.95
    assert target_migration_status(session, low, now=now)["ready_for_llm"] is False
    assert target_migration_status(session, early, now=now)["ready_for_llm"] is False
    assert target_migration_status(session, old, now=now)["ready_for_llm"] is True
    assert apply_migration(session, now=now) == ["exact-95", "seven-days"]
    assert exact.extraction_method == "llm"
    assert low.extraction_method == "dual"
    assert early.extraction_method == "dual"
    assert old.extraction_method == "llm"


def test_openrouter_and_302_are_switched_last(session):
    now = datetime(2026, 11, 12, tzinfo=UTC)
    for name in ("OpenRouter", "302.AI", "simple"):
        target = _target(name, now - timedelta(days=8))
        _samples(session, target, 1, 1)
    session.commit()
    report = migration_report(session, now=now)
    assert [item["name"] for item in report] == ["simple", "OpenRouter", "302.AI"]
    assert apply_migration(session, now=now) == ["simple", "OpenRouter", "302.AI"]


def test_seed_marks_three_migrated_sites_and_others_dual(session):
    seed_targets(session)
    assert session.get(Target, 1).extraction_method == "dual"
    for name in ("AiHubMix", "硅基流动 SiliconFlow", "API2D"):
        target = next(item for item in session.query(Target).all() if item.name == name)
        assert target.extraction_method == "llm"
    assert session.query(Target).count() == 26


def test_long_text_crop_keeps_price_block():
    text = "公司历史与品牌介绍。\n" * 300 + "\n价格表：模型 demo 输入 1.00 输出 2.00 USD 每百万token"
    result = crop_pricing_text(text, 120)
    assert len(result) <= 120
    assert "价格表" in result
    assert "1.00" in result
