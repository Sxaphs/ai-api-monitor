from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.collectors.http import FetchError, FetchResult
from app.collectors.sites import SiteCollector
from app.db import get_db
from app.main import create_app
from app.models import Change, Snapshot, Target
from app.services.changes import diff_pricing
from app.services.reports import ReportService
from app.time_utils import utc_now


class StubFetcher:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def fetch(self, url, *, method="http", headers=None):
        self.calls.append((url, headers))
        value = self.responses[url]
        if isinstance(value, list):
            value = value.pop(0)
        if isinstance(value, Exception):
            raise value
        return FetchResult(text=value, status_code=200, url=url, content_type="text/html")


class StubLLM:
    def __init__(self, results):
        self.results = results

    def extract(self, source_text):
        value = self.results.pop(0)
        return {
            "models": [{"name": name, "input_price": None, "output_price": None, "currency": None,
                        "unit": None, "tier_or_group": None, "note": None} for name in value],
            "page_level": {"exchange_rate": None, "minimum_recharge": None, "discount_rules": None},
            "parse_ok": True, "validation_pending": False, "invalid_fields": [], "warnings": [],
        }


def make_client(session_factory):
    app = create_app(start_scheduler=False, initialize_database=False)

    def override_db():
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


def snapshot(target_id, models, *, pending=False, kind="pricing"):
    return Snapshot(
        target_id=target_id, kind=kind, status="success", parse_ok=True,
        raw_content="测试原文", extracted_json={
            "models": models, "page_level": {}, "validation_pending": pending,
            "invalid_fields": [{"field": "models[0].input_price"}] if pending else [],
        },
    )


@pytest.mark.parametrize("new_price,expected_status", [
    (None, "pending"),       # 原文数字校验失败后字段被置 null
    ("1.50", "confirmed"),  # 正好 50%，不强制 pending
    ("3.00", "pending"),    # 200%，强制 pending
])
def test_price_verification_rules_and_review_page(
    session_factory, session, new_price, expected_status
):
    target = Target(name=f"verify-{new_price}", group_code="A", pricing_mode="llm")
    session.add(target)
    session.flush()
    previous = snapshot(target.id, [{"name": "demo", "input_price": "1.00", "unit": "每百万token"}])
    current = snapshot(
        target.id, [{"name": "demo", "input_price": new_price, "unit": "每百万token"}],
        pending=new_price is None,
    )
    session.add_all([previous, current])
    session.flush()
    changes = diff_pricing(session, target=target, previous=previous, current=current)
    session.commit()
    assert len(changes) == 1
    assert changes[0].verify_status == expected_status
    response = make_client(session_factory).get("/review")
    assert response.status_code == 200
    assert ("demo 输入价调整" in response.text) == (expected_status == "pending")


@pytest.mark.parametrize("with_key,api_status", [(False, None), (True, 401), (True, 403)])
def test_missing_key_and_auth_failures_use_separate_webpage_baseline(
    session_factory, session, monkeypatch, with_key, api_status
):
    if with_key:
        monkeypatch.setenv("TEST_SF_KEY", "fake-secret")
    else:
        monkeypatch.delenv("TEST_SF_KEY", raising=False)
    target = Target(
        name=f"sf-fallback-{with_key}-{api_status}", group_code="A",
        models_url="https://api.siliconflow.cn/v1/models",
        models_fallback_url="https://docs.siliconflow.cn/quickstart/models",
        auth_type="bearer", auth_env_key="TEST_SF_KEY",
    )
    session.add(target)
    session.commit()
    api_value = FetchError(f"HTTP {api_status}", status_code=api_status) if api_status else '{"data":[]}'
    fetcher = StubFetcher({
        target.models_url: api_value,
        target.models_fallback_url: "<html>公开模型列表</html>",
    })
    llm = StubLLM([["web-a"], ["web-a", "web-b"]])
    collector = SiteCollector(session_factory, fetcher, llm)
    collector.collect_all()
    collector.collect_all()

    changes = session.scalars(select(Change).where(Change.target_id == target.id)).all()
    assert len(changes) == 1
    assert changes[0].change_type == "新模型"
    assert changes[0].new_value == "web-b"
    assert changes[0].data_source == "webpage"
    kinds = session.scalars(select(Snapshot.kind).where(Snapshot.target_id == target.id)).all()
    assert kinds.count("models_webpage") == 2
    assert kinds.count("models") == 2  # 两次 API 失败快照，不与 webpage 混 diff
    if with_key:
        assert fetcher.calls[0][1] == {"Authorization": "Bearer fake-secret"}
    else:
        assert all(call[0] != target.models_url for call in fetcher.calls)


def test_configured_key_uses_api_and_marks_change_source(session_factory, session, monkeypatch):
    monkeypatch.setenv("TEST_SF_KEY", "fake-secret")
    target = Target(
        name="sf-api", group_code="A", models_url="https://api.siliconflow.cn/v1/models",
        models_fallback_url="https://docs.siliconflow.cn/quickstart/models",
        auth_type="bearer", auth_env_key="TEST_SF_KEY",
    )
    session.add(target)
    session.commit()
    fetcher = StubFetcher({
        target.models_url: ['{"data":[{"id":"api-a"}]}', '{"data":[{"id":"api-a"},{"id":"api-b"}]}'],
    })
    collector = SiteCollector(session_factory, fetcher, StubLLM([]))
    collector.collect_all()
    collector.collect_all()
    change = session.scalar(select(Change).where(Change.target_id == target.id))
    assert change.new_value == "api-b"
    assert change.data_source == "api"
    assert all(call[0] == target.models_url for call in fetcher.calls)


def test_302_metadata_diff_and_price_missing_labels(session_factory, session):
    target = Target(
        name="302.AI-test", group_code="A", pricing_url="https://302.ai/pricing",
        pricing_mode="metadata_only", price_data_missing=True, extraction_method="parser",
    )
    session.add(target)
    session.commit()
    first = "<div>计费方式：按量计费</div><div>汇率口径：1 USD = 7.20 CNY</div><div>充值送 5%</div>"
    second = "<div>计费方式：按量计费</div><div>汇率口径：1 USD = 7.30 CNY</div><div>充值送 5%</div>"
    collector = SiteCollector(session_factory, StubFetcher({target.pricing_url: [first, second]}), StubLLM([]))
    collector.collect_all()
    collector.collect_all()
    change = session.scalar(select(Change).where(Change.target_id == target.id))
    assert change.change_type == "口径变化"
    assert "汇率口径" in change.summary
    assert change.verify_status == "confirmed"

    page = make_client(session_factory).get("/changes")
    assert "价格暂缺" in page.text
    now = utc_now()
    content = ReportService(session).generate(now - timedelta(days=1), now + timedelta(days=1)).content
    assert "302.AI-test：该站价格表需登录，本期暂缺，仅监测口径变化" in content
    assert "汇率口径变化" in content


def test_llm_failure_still_saves_raw_html(session_factory, session):
    class FailingLLM:
        def extract(self, source_text):
            raise RuntimeError("LLM unavailable")

    html = "<main>模型 demo 输入价 1.00</main>"
    target = Target(
        name="llm-failure", group_code="A", pricing_url="https://example.test/pricing",
        pricing_mode="llm",
    )
    session.add(target)
    session.commit()
    stats = SiteCollector(
        session_factory, StubFetcher({target.pricing_url: html}), FailingLLM()
    ).collect_all()
    assert stats["failed"] == 1
    snapshot = session.scalar(select(Snapshot).where(Snapshot.target_id == target.id))
    assert snapshot.status == "failed"
    assert snapshot.raw_content == html
    assert "LLM unavailable" in snapshot.error


def test_missing_and_empty_metadata_do_not_create_false_change(session):
    target = Target(name="empty-metadata", group_code="A")
    session.add(target)
    session.flush()
    previous = Snapshot(
        target_id=target.id, kind="pricing", status="success", parse_ok=True,
        extracted_json={"models": {}},
    )
    current = Snapshot(
        target_id=target.id, kind="pricing", status="success", parse_ok=True,
        extracted_json={"models": {}, "billing_method": [], "exchange_rate": []},
    )
    session.add_all([previous, current])
    session.flush()
    assert diff_pricing(session, target=target, previous=previous, current=current) == []
