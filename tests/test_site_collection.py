from __future__ import annotations

import json

from sqlalchemy import select

from app.collectors.http import FetchResult
from app.collectors.sites import SiteCollector
from app.models import Change, Snapshot, Target


def pricing_html(input_price: str = "1.00", output_price: str = "2.00") -> str:
    return f"""
    <html><body>
      <div>汇率口径：1 USD = 7.2 CNY</div><div>最低充值 10 元</div>
      <table>
        <tr><th>模型</th><th>输入单价（USD/1M）</th><th>输出单价（USD/1M）</th><th>分组</th><th>倍率</th></tr>
        <tr><td>gpt-test</td><td>${input_price}</td><td>${output_price}</td><td>default</td><td>1</td></tr>
      </table>
    </body></html>
    """


class StubFetcher:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def fetch(self, url, *, method="http", headers=None):
        self.calls.append((url, method))
        value = self.responses[url]
        if isinstance(value, list):
            value = value.pop(0)
        if isinstance(value, Exception):
            raise value
        return FetchResult(text=value, status_code=200, url=url, content_type="text/html")


def test_failed_site_does_not_block_and_failed_snapshot_is_not_baseline(session_factory, session):
    session.add_all([
        Target(name="good", group_code="A", pricing_url="https://good.test/pricing", extraction_method="parser"),
        Target(name="bad", group_code="B", pricing_url="https://bad.test/pricing", extraction_method="parser"),
    ])
    session.commit()
    fetcher = StubFetcher({
        "https://good.test/pricing": [pricing_html(), RuntimeError("temporary"), pricing_html("1.25")],
        "https://bad.test/pricing": RuntimeError("offline"),
    })
    collector = SiteCollector(session_factory, fetcher)

    first = collector.collect_all()
    assert first["success"] == 1
    assert first["failed"] == 1
    assert first["changes"] == 0

    second = collector.collect_all()
    assert second["failed"] == 2
    third = collector.collect_all()
    assert third["success"] == 1
    assert third["failed"] == 1

    good = session.scalar(select(Target).where(Target.name == "good"))
    snapshots = session.scalars(select(Snapshot).where(Snapshot.target_id == good.id).order_by(Snapshot.id)).all()
    assert [item.status for item in snapshots] == ["success", "failed", "success"]
    change = session.scalar(select(Change).where(Change.target_id == good.id))
    assert change.change_type == "降价" or change.change_type == "涨价"
    assert change.change_type == "涨价"
    assert change.old_value == "1 USD per_1m_tokens"
    assert change.new_value == "1.25 USD per_1m_tokens"
    assert float(change.change_percent) == 25.0
    assert "模型下线" not in {item.change_type for item in session.scalars(select(Change)).all()}


def test_model_list_added_removed_and_repeat_has_no_diff(session_factory, session):
    target = Target(name="models", group_code="A", models_url="https://models.test/v1/models")
    session.add(target)
    session.commit()
    fetcher = StubFetcher({
        target.models_url: [
            json.dumps({"data": [{"id": "model-a"}, {"id": "model-b"}]}),
            json.dumps({"data": [{"id": "model-b"}, {"id": "model-c"}]}),
            json.dumps({"data": [{"id": "model-b"}, {"id": "model-c"}]}),
        ]
    })
    collector = SiteCollector(session_factory, fetcher)
    assert collector.collect_all()["changes"] == 0
    assert collector.collect_all()["changes"] == 2
    assert collector.collect_all()["changes"] == 0
    changes = session.scalars(select(Change).order_by(Change.change_type, Change.id)).all()
    assert {(item.change_type, item.new_value or item.old_value) for item in changes} == {
        ("新模型", "model-c"), ("模型下线", "model-a")
    }
