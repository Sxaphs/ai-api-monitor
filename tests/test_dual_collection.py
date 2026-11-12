from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.collectors.http import FetchResult
from app.collectors.sites import SiteCollector
from app.db import get_db
from app.main import create_app
from app.models import Change, Snapshot, Target

HTML = """
<html><body><div>汇率口径：1 USD = 7.20 CNY</div><table>
<tr><th>模型</th><th>输入单价（USD/1M）</th><th>输出单价（USD/1M）</th><th>分组</th><th>倍率</th></tr>
<tr><td>demo</td><td>$1.00</td><td>$2.00</td><td>default</td><td>1</td></tr>
</table></body></html>
"""


class FetcherStub:
    def fetch(self, url, *, method="http", headers=None):
        return FetchResult(text=HTML, status_code=200, url=url, content_type="text/html")


class LLMStub:
    def __init__(self, prices):
        self.prices = prices

    def extract(self, source_text):
        input_price, output_price = self.prices.pop(0)
        return {
            "models": [{"name": "demo", "input_price": input_price, "output_price": output_price,
                        "currency": "USD", "unit": "每百万token", "tier_or_group": "default", "note": None}],
            "page_level": {"exchange_rate": "汇率口径：1 USD = 7.20 CNY", "minimum_recharge": None, "discount_rules": None},
            "parse_ok": True, "validation_pending": False, "invalid_fields": [], "warnings": [],
            "llm_usage": {"calls": 1, "estimated_input_tokens": 100, "estimated_output_tokens": 20,
                          "estimated_total_tokens": 120, "estimation_method": "test"},
        }


def client(session_factory):
    app = create_app(start_scheduler=False, initialize_database=False)
    def override():
        with session_factory() as session:
            yield session
    app.dependency_overrides[get_db] = override
    return TestClient(app)


def test_dual_stores_both_results_mismatch_enters_review_and_usage_aggregates(session_factory, session):
    target = Target(name="dual-site", group_code="A", pricing_url="https://dual.test/pricing", extraction_method="dual")
    session.add(target)
    session.commit()
    collector = SiteCollector(session_factory, FetcherStub(), LLMStub([("1.00", "2.00"), ("1.10", "2.00")]))

    collector.collect_all()
    first = session.scalar(select(Snapshot).where(Snapshot.target_id == target.id))
    assert first.dual_match is True
    assert first.dual_successful is True
    assert isinstance(first.extracted_json["models"], list)
    assert isinstance(first.parser_extracted_json["models"], dict)
    assert first.llm_calls == 1
    assert first.llm_estimated_tokens == 120

    collector.collect_all()
    latest = session.scalar(select(Snapshot).where(Snapshot.target_id == target.id).order_by(Snapshot.id.desc()).limit(1))
    assert latest.dual_match is False
    assert latest.dual_successful is True
    assert latest.dual_difference_fields[0]["field"] == "models[demo].input_price"
    changes = session.scalars(select(Change).where(Change.target_id == target.id)).all()
    assert len(changes) == 2
    assert all(change.verify_status == "pending" for change in changes)
    mismatch = next(change for change in changes if change.change_type == "提取分歧")
    assert "models[demo].input_price" in mismatch.summary
    assert mismatch.extra_json["dual_snapshot_id"] == latest.id
    price = next(change for change in changes if change.change_type == "涨价")
    assert price.old_value.startswith("1.00 ") and price.new_value.startswith("1.10 ")
    assert price.extra_json["dual_snapshot_id"] == latest.id

    web = client(session_factory)
    review = web.get("/review")
    assert "双跑提取不一致" in review.text and "demo 输入价调整" in review.text
    assert web.post(f"/review/{mismatch.id}/confirm", follow_redirects=False).status_code == 303
    session.expire_all()
    assert session.get(Snapshot, latest.id).dual_llm_confirmed is True

    changes_page = web.get("/changes")
    assert "LLM 提取用量" in changes_page.text
    assert "dual-site" in changes_page.text
    assert "240" in changes_page.text
