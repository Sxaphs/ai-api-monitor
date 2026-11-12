from __future__ import annotations

import json
import re
from typing import Any

from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field

from ..config import Settings, settings
from .http import FetchError, Fetcher

NUMBER_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


class PricingModelItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    input_price: str | None = None
    output_price: str | None = None
    currency: str | None = None
    unit: str | None = None
    tier_or_group: str | None = None
    note: str | None = None


class PageLevel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    exchange_rate: str | None = None
    minimum_recharge: str | None = None
    discount_rules: str | None = None


class LLMPricingResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    models: list[PricingModelItem] = Field(default_factory=list)
    page_level: PageLevel = Field(default_factory=PageLevel)


def clean_html_for_llm(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["script", "style", "nav", "footer"]):
        tag.decompose()
    lines = []
    for raw_line in soup.get_text("\n", strip=True).splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


PRICING_BLOCK_RE = re.compile(
    r"价格|定价|计费|模型|输入|输出|充值|赠金|折扣|pricing|price|billing|input|output|token|\$|¥",
    re.IGNORECASE,
)


def crop_pricing_text(source_text: str, max_chars: int) -> str:
    """超限时选择价格信息密度最高的窗口，而不是固定截掉正文尾部。"""
    if len(source_text) <= max_chars:
        return source_text
    best_start = 0
    best_score = -1
    for match in PRICING_BLOCK_RE.finditer(source_text):
        start = max(0, match.start() - max_chars // 3)
        if start + max_chars > len(source_text):
            start = len(source_text) - max_chars
        window = source_text[start : start + max_chars]
        score = len(PRICING_BLOCK_RE.findall(window))
        if score > best_score:
            best_start, best_score = start, score
    return source_text[best_start : best_start + max_chars]


def estimate_tokens(text: str) -> int:
    """无 tokenizer 时按 UTF-8 字节数 / 3 粗略估算，仅供用量观察。"""
    return (len(text.encode("utf-8")) + 2) // 3 if text else 0


def _number_in_source(token: str, source_text: str) -> bool:
    # 防止 9.99 错误命中 19.990 这类长数字片段。
    pattern = rf"(?<![\d.,]){re.escape(token)}(?![\d.,])"
    return re.search(pattern, source_text) is not None


def validate_numbers_against_source(
    result: LLMPricingResult | dict[str, Any], source_text: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """任一字段含原文不存在的数字词法时，将整个字段置 null。"""
    data = result.model_dump() if isinstance(result, LLMPricingResult) else result
    invalid: list[dict[str, Any]] = []

    def walk(value: Any, path: str) -> Any:
        if isinstance(value, dict):
            return {key: walk(child, f"{path}.{key}" if path else key) for key, child in value.items()}
        if isinstance(value, list):
            return [walk(child, f"{path}[{index}]") for index, child in enumerate(value)]
        if isinstance(value, bool) or value is None:
            return value
        if isinstance(value, (int, float)):
            value = str(value)
        if not isinstance(value, str):
            return value
        missing = [token for token in NUMBER_RE.findall(value) if not _number_in_source(token, source_text)]
        if missing:
            invalid.append({"field": path, "numbers": missing, "value": value})
            return None
        return value

    validated = walk(data, "")
    return validated, invalid


class LLMExtractor:
    def __init__(self, config: Settings = settings, fetcher: Fetcher | None = None) -> None:
        self.config = config
        self.fetcher = fetcher or Fetcher(config)

    def extract(self, source_text: str) -> dict[str, Any]:
        if not self.config.llm_api_key:
            raise RuntimeError("未配置 LLM_API_KEY，无法执行定价页 LLM 提取")
        llm_input = crop_pricing_text(source_text, self.config.llm_max_chars)
        prompt = f"""
请把下面的定价页原文提取为 JSON。只复制原文明确存在的信息，禁止推测、换算或补齐。
数字必须逐字复制，例如原文是 1.00 就不得写成 1.0 或 1。
没有可靠信息的字段填 null，没有模型时 models 填 []。

固定 JSON 结构：
{{
  "models": [
    {{"name": "模型名", "input_price": "输入价", "output_price": "输出价",
      "currency": "币种", "unit": "每百万token|每千次|每次",
      "tier_or_group": "分组或倍率", "note": "必要备注"}}
  ],
  "page_level": {{"exchange_rate": "汇率口径", "minimum_recharge": "最低充值",
    "discount_rules": "赠金或折扣规则"}}
}}

页面原文：
{llm_input}
""".strip()
        response = self.fetcher.post_json(
            f"{self.config.llm_base_url}/chat/completions",
            payload={
                "model": self.config.llm_model,
                "messages": [
                    {"role": "system", "content": "你是严谨的定价页结构化提取器，只输出 JSON。"},
                    {"role": "user", "content": prompt},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0,
            },
            headers={"Authorization": f"Bearer {self.config.llm_api_key}"},
        )
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise FetchError("LLM 响应缺少 choices[0].message.content") from exc
        if not isinstance(content, str):
            raise FetchError("LLM 返回的 content 不是字符串")
        content = content.strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.IGNORECASE)
        try:
            # parse_float/parse_int=str 保留数字原始写法供一致性校验。
            raw = json.loads(content, parse_float=str, parse_int=str)
            parsed = LLMPricingResult.model_validate(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            raise FetchError(f"LLM 输出不符合固定 JSON schema: {exc}") from exc

        validated, invalid = validate_numbers_against_source(parsed, source_text)
        warnings = []
        if invalid:
            warnings.append(f"{len(invalid)} 个字段未通过原文数字一致性校验")
        if len(source_text) > self.config.llm_max_chars:
            warnings.append(f"原文按价格区块优先裁剪至 {self.config.llm_max_chars} 字符")
        page_level = validated.get("page_level", {})
        input_estimate = estimate_tokens(prompt)
        output_estimate = estimate_tokens(content)
        actual_usage = response.get("usage") if isinstance(response.get("usage"), dict) else None
        return {
            **validated,
            "parse_ok": bool(validated.get("models") or any(page_level.values())),
            "validation_pending": bool(invalid),
            "invalid_fields": invalid,
            "warnings": warnings,
            "source_chars": len(source_text),
            "llm_input_chars": len(llm_input),
            "llm_usage": {
                "calls": 1,
                "model": self.config.llm_model,
                "estimated_input_tokens": input_estimate,
                "estimated_output_tokens": output_estimate,
                "estimated_total_tokens": input_estimate + output_estimate,
                "estimation_method": "utf8_bytes_div_3",
                "provider_usage": actual_usage,
            },
        }
