from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from app.collectors.http import Fetcher
from app.config import Settings
from app.services.reports import ReportService
from app.services.summaries import ReportSummarizer

UTC = timezone.utc


def summary_config() -> Settings:
    return Settings(
        database_url="sqlite://",
        rapidapi_key=None,
        scheduler_enabled=False,
        log_level="INFO",
        llm_api_key="test-key",
        retry_attempts=1,
    )


def test_report_summarizer_calls_openai_compatible_api():
    requests = []

    def handler(request: httpx.Request):
        requests.append(request)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "本期有价格调整和功能上线。"}}]
        })

    config = summary_config()
    summarizer = ReportSummarizer(
        config, fetcher=Fetcher(config, transport=httpx.MockTransport(handler))
    )
    result = summarizer.summarize("# 报告\n\n- 变动总数：2 条")
    assert result == "本期有价格调整和功能上线。"
    assert len(requests) == 1
    assert requests[0].headers["authorization"] == "Bearer test-key"
    body = requests[0].content.decode("utf-8")
    assert "变动总数：2 条" in body
    assert "不添加评价、建议、预测" in body


def test_report_summary_is_inserted_before_overview_and_failure_is_explicit(session):
    class StubSummarizer:
        content = ""

        def summarize(self, content):
            self.content = content
            return "这是基于报告事实的模型摘要。"

    start = datetime(2026, 11, 1, tzinfo=UTC)
    summarizer = StubSummarizer()
    report = ReportService(session, summarizer=summarizer).generate(
        start, start + timedelta(days=7), persist=False
    )
    assert "【模型总结】" in report.content
    assert report.content.index("【模型总结】") < report.content.index("【本期概览】")
    assert "这是基于报告事实的模型摘要。" in report.content
    assert "变动总数：0 条" in summarizer.content

    class FailingSummarizer:
        def summarize(self, content):
            raise RuntimeError("test network failure")

    fallback = ReportService(session, summarizer=FailingSummarizer()).generate(
        start, start + timedelta(days=7), persist=False
    )
    assert "模型总结生成失败" in fallback.content
    assert "下方为原始统计数据" in fallback.content
