from __future__ import annotations

import json

import httpx
import pytest

from app.collectors.http import Fetcher
from app.collectors.llm import LLMExtractor, clean_html_for_llm
from app.config import Settings


def llm_config():
    return Settings(
        database_url="sqlite://", rapidapi_key=None, scheduler_enabled=False, log_level="INFO",
        retry_backoff=0, llm_base_url="https://llm.test/v1", llm_model="cheap-test",
        llm_api_key="test-llm-key",
    )


def llm_payload(input_price, output_price, name="demo-v2", unit="每百万token", minimum="10 元"):
    return {
        "models": [{
            "name": name, "input_price": input_price, "output_price": output_price,
            "currency": "USD", "unit": unit, "tier_or_group": "default", "note": None,
        }],
        "page_level": {
            "exchange_rate": "1 USD = 7.20 CNY", "minimum_recharge": minimum,
            "discount_rules": "充100送10",
        },
    }


@pytest.mark.parametrize("site,marker,name,input_price,output_price,unit,minimum", [
    ("AiHubMix", "AIHUB-2026", "demo-v2", "1.00", "2.00", "每百万token", "10 元"),
    ("SiliconFlow", "SILICON-2026", "silicon-1.5", "0.50", "1.00", "每百万token", "20 元"),
    ("API2D", "API2D-2026", "api-2", "0.002", "0.004", "每千次", "30 元"),
])
def test_llm_schema_and_source_number_validation(
    site, marker, name, input_price, output_price, unit, minimum
):
    expected = llm_payload(input_price, output_price, name, unit, minimum)
    html = f"""
    <html><nav>不应发送 999</nav><style>888</style><script>777</script>
    <main>{site} {marker} 模型 {name} 输入价 {input_price} 输出价 {output_price}
    USD {unit} default 汇率口径 1 USD = 7.20 CNY 最低充值 {minimum} 赠金 充100送10</main>
    <footer>666</footer></html>
    """
    cleaned = clean_html_for_llm(html)
    assert "999" not in cleaned and "888" not in cleaned and "777" not in cleaned and "666" not in cleaned
    assert marker in cleaned

    def handler(request):
        if request.url.host == "llm.test":
            body = json.loads(request.content)
            assert body["model"] == "cheap-test"
            assert body["response_format"] == {"type": "json_object"}
            assert marker in body["messages"][1]["content"]
            assert "999" not in body["messages"][1]["content"]
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(expected, ensure_ascii=False)}}]})
        raise AssertionError("本测试只直接调用 LLM")

    fetcher = Fetcher(llm_config(), sleep=lambda _: None, transport=httpx.MockTransport(handler))
    result = LLMExtractor(llm_config(), fetcher).extract(cleaned)
    assert result["models"] == expected["models"]
    assert result["page_level"] == expected["page_level"]
    assert result["parse_ok"] is True
    assert result["validation_pending"] is False
    assert result["invalid_fields"] == []


def test_hallucinated_number_is_nulled_without_blocking_result():
    expected = llm_payload("9.99", "2.00")

    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(expected, ensure_ascii=False)}}]})

    fetcher = Fetcher(llm_config(), sleep=lambda _: None, transport=httpx.MockTransport(handler))
    source = "模型 demo-v2 输入 19.990 输出 2.00 汇率 1 USD = 7.20 CNY 最低充值 10 元 充100送10"
    result = LLMExtractor(llm_config(), fetcher).extract(source)
    assert result["models"][0]["input_price"] is None
    assert result["models"][0]["output_price"] == "2.00"
    assert result["validation_pending"] is True
    assert result["invalid_fields"][0]["field"] == "models[0].input_price"
    assert result["invalid_fields"][0]["numbers"] == ["9.99"]
