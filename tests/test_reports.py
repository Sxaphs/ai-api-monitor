from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.models import Report, Target
from app.services.changes import add_change
from app.services.reports import SECTION_TITLES, ReportService, recent_report_window

UTC = timezone.utc


def test_period_report_five_sections_prices_activities_and_warnings(session):
    start = datetime(2026, 10, 12, 16, tzinfo=UTC)
    end = start + timedelta(days=30)
    alpha = Target(name="Alpha", group_code="A")
    metadata = Target(name="302.AI", group_code="A", price_data_missing=True)
    upstream = Target(name="Upstream", group_code="D")
    session.add_all([alpha, metadata, upstream])
    session.commit()

    add_change(
        session, target=alpha, snapshot=None, change_type="涨价",
        summary="gpt-test 输入价调整", old_value="1 USD", new_value="1.25 USD",
        change_percent=Decimal("25"), verify_status="confirmed",
        source_url="https://alpha.test/pricing", detected_at=start + timedelta(hours=2),
        extra_json={"model": "gpt-test 输入价"},
    )
    add_change(
        session, target=alpha, snapshot=None, change_type="新模型", summary="上线 gpt-test",
        verify_status="confirmed", source_url="https://alpha.test/models",
        detected_at=start + timedelta(hours=3),
    )
    add_change(
        session, target=upstream, snapshot=None, change_type="模型下线", summary="下线 old-model",
        verify_status="confirmed", severity="high", source_url="https://upstream.test/eol",
        detected_at=start + timedelta(hours=4),
    )
    add_change(
        session, target=alpha, snapshot=None, change_type="营销活动", summary="充值送活动",
        verify_status="confirmed", source_url="https://alpha.test/campaign",
        detected_at=start + timedelta(hours=5),
    )
    add_change(
        session, target=metadata, snapshot=None, change_type="口径变化", summary="计费口径调整",
        verify_status="pending", source_url="https://302.ai/pricing",
        detected_at=start + timedelta(hours=6),
    )
    add_change(
        session, target=alpha, snapshot=None, change_type="新功能", summary="误报功能不得出现",
        verify_status="false_alarm", source_url="https://alpha.test/false",
        detected_at=start + timedelta(hours=7),
    )
    add_change(
        session, target=alpha, snapshot=None, change_type="X公告",
        summary="多行公告第一行\n\n多行公告第二行", verify_status="confirmed",
        source_url="https://x.com/a/status/multiline", detected_at=start + timedelta(hours=8),
    )

    landed_warning = add_change(
        session, target=alpha, snapshot=None, change_type="新功能", summary="新功能预告",
        signal_type="warning", severity="medium", source_url="https://x.com/a/status/1",
        detected_at=start + timedelta(days=1),
    )
    landed = add_change(
        session, target=alpha, snapshot=None, change_type="新功能", summary="新功能已经上线",
        verify_status="confirmed", source_url="https://alpha.test/feature",
        detected_at=start + timedelta(days=2),
    )
    assert landed_warning.related_id == landed.id
    add_change(
        session, target=alpha, snapshot=None, change_type="价格变动", summary="超时预警",
        signal_type="warning", detected_at=start + timedelta(days=3),
    )
    add_change(
        session, target=alpha, snapshot=None, change_type="新功能", summary="观察期预警",
        signal_type="warning", detected_at=end - timedelta(days=1),
    )
    add_change(
        session, target=alpha, snapshot=None, change_type="X公告", summary="误报警告不得出现",
        signal_type="warning", verify_status="false_alarm", detected_at=start + timedelta(days=4),
    )

    report = ReportService(session).generate(
        start, end, title="AI API 中转站竞品监测月报（近 30 天）", report_kind="monthly"
    )
    content = report.content
    for title in SECTION_TITLES:
        assert f"## {title}" in content
    assert report.id is not None
    assert report.report_kind == "monthly"
    assert "| 站点 | 模型 | 旧价 | 新价 | 幅度 | 日期 |" in content
    assert "| Alpha | gpt-test 输入价 | 1 USD | 1.25 USD | +25.00% |" in content
    assert "302.AI：该站价格表需登录，本期暂缺，仅监测口径变化" in content
    assert "### Alpha" in content
    assert "上线 gpt-test" in content and "下线 old-model" in content
    assert "充值送活动；[原文](https://alpha.test/campaign)" in content
    assert "计费口径调整（待人工核对）；[原文](https://302.ai/pricing)" in content
    assert "多行公告第一行 多行公告第二行；[原文](https://x.com/a/status/multiline)" in content
    assert "多行公告第一行\n" not in content
    assert "预警落地率：25.00%（1/4）" in content
    assert "落地 1 条；误报 1 条；超时未落地 1 条；仍在观察期 1 条" in content
    assert "预警「新功能预告」→ 落地「新功能已经上线」" in content
    assert "[预警原文](https://x.com/a/status/1)" in content
    assert "误报功能不得出现" not in content
    assert "误报警告不得出现" not in content


def test_empty_period_keeps_titles_and_recent_windows(session):
    start = datetime(2026, 10, 12, 16, tzinfo=UTC)
    end = start + timedelta(days=7)
    report = ReportService(session).generate(
        start, end, title="AI API 中转站竞品监测周报（近 7 天）", report_kind="weekly"
    )
    assert "变动总数：0 条" in report.content
    assert "涉及站点数：0 个" in report.content
    assert "预警落地率：本期无预警" in report.content
    assert report.content.splitlines().count("本期无") == 4
    for title in SECTION_TITLES:
        assert f"## {title}" in report.content

    now = datetime(2026, 11, 12, 4, tzinfo=UTC)
    week_start, week_end = recent_report_window(7, now)
    month_start, month_end = recent_report_window(30, now)
    assert week_end == now and week_start == now - timedelta(days=7)
    assert month_end == now and month_start == now - timedelta(days=30)
    assert session.query(Report).count() == 1
