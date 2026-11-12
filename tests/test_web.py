from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.collectors.twitter import TwitterCollector
from app.db import get_db
from app.main import create_app
from app.models import Change, Report, Target
from app.services.changes import add_change

UTC = timezone.utc


def make_client(session_factory):
    app = create_app(start_scheduler=False, initialize_database=False)

    def override_db():
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


def test_review_confirm_false_alarm_and_filters(session_factory, session):
    target = Target(name="web-target", group_code="A")
    other = Target(name="other-target", group_code="B")
    session.add_all([target, other])
    session.commit()
    first = add_change(session, target=target, snapshot=None, change_type="新功能", summary="待确认的唯一摘要", signal_type="warning")
    second = add_change(session, target=other, snapshot=None, change_type="营销活动", summary="待误报的唯一摘要", signal_type="warning")
    session.commit()
    client = make_client(session_factory)

    response = client.get("/review")
    assert response.status_code == 200
    assert "待确认的唯一摘要" in response.text
    assert "待误报的唯一摘要" in response.text
    assert client.post(f"/review/{first.id}/confirm", follow_redirects=False).status_code == 303
    assert client.post(f"/review/{second.id}/false-alarm", follow_redirects=False).status_code == 303
    session.expire_all()
    assert session.get(Change, first.id).verify_status == "confirmed"
    assert session.get(Change, second.id).verify_status == "false_alarm"
    assert "待确认的唯一摘要" not in client.get("/review").text

    filtered = client.get("/changes?group=A&change_type=新功能&signal_type=warning")
    assert filtered.status_code == 200
    assert "待确认的唯一摘要" in filtered.text
    assert "待误报的唯一摘要" not in filtered.text


def test_targets_crud_and_toggle(session_factory):
    client = make_client(session_factory)
    created = client.post("/api/targets", json={
        "name": "new-target", "group_code": "A", "pricing_url": "https://example.com/pricing",
        "x_handle": "@example",
    })
    assert created.status_code == 201
    target_id = created.json()["id"]
    assert created.json()["x_since"] is not None
    assert created.json()["extraction_method"] == "dual"
    assert created.json()["dual_started_at"] is not None

    updated = client.patch(f"/api/targets/{target_id}", json={"pricing_url": "", "collect_method": "playwright", "extraction_method": "llm"})
    assert updated.status_code == 200
    assert updated.json()["pricing_url"] is None
    assert updated.json()["collect_method"] == "playwright"
    toggled = client.post(f"/api/targets/{target_id}/toggle")
    assert toggled.json()["enabled"] is False
    assert client.delete(f"/api/targets/{target_id}").status_code == 204
    assert client.get("/api/targets").json() == []


def test_manual_weekly_monthly_custom_reports_and_history(session_factory, session):
    client = make_client(session_factory)
    assert client.get("/reports").status_code == 200

    weekly = client.post("/reports", data={"preset": "7"}, follow_redirects=False)
    assert weekly.status_code == 303
    weekly_id = int(weekly.headers["location"].rsplit("/", 1)[1])
    session.expire_all()
    weekly_report = session.get(Report, weekly_id)
    assert weekly_report.title == "AI API 中转站竞品监测周报（近 7 天）"
    assert weekly_report.report_kind == "weekly"
    detail = client.get(weekly.headers["location"])
    assert detail.status_code == 200
    assert "【本期概览】" in detail.text and "【预警回顾】" in detail.text
    assert "本期无" in detail.text

    monthly = client.post("/reports", data={"preset": "30"}, follow_redirects=False)
    assert monthly.status_code == 303
    custom = client.post(
        "/reports",
        data={"preset": "custom", "start_date": "2026-10-12", "end_date": "2026-10-12"},
        follow_redirects=False,
    )
    assert custom.status_code == 303
    history = client.get("/reports")
    assert "近 7 天" in history.text and "近 30 天" in history.text
    assert "2026-10-12 至 2026-10-12" in history.text

    invalid = client.post(
        "/reports",
        data={"preset": "custom", "start_date": "2026-10-13", "end_date": "2026-10-12"},
    )
    assert invalid.status_code == 422
    assert "开始日期不能晚于结束日期" in invalid.text
    assert client.get("/reports/999999").status_code == 404


def test_report_detail_escapes_raw_html(session_factory, session):
    report = Report(
        title="安全测试",
        report_kind="custom",
        period_start=datetime(2026, 11, 1, tzinfo=timezone.utc),
        period_end=datetime(2026, 11, 2, tzinfo=timezone.utc),
        content="# 安全测试\n\n<script>alert('xss')</script>\n",
    )
    session.add(report)
    session.commit()
    response = make_client(session_factory).get(f"/reports/{report.id}")
    assert response.status_code == 200
    assert "<script>alert('xss')</script>" not in response.text
    assert "&lt;script&gt;" in response.text
