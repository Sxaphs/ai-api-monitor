from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from bs4 import BeautifulSoup

MODEL_HEADERS = ("model", "model name", "模型", "模型名称", "名称")
INPUT_HEADERS = ("input", "prompt", "输入", "输入价", "输入单价")
OUTPUT_HEADERS = ("output", "completion", "输出", "输出价", "输出单价")
GROUP_HEADERS = ("group", "分组", "组别")
MULTIPLIER_HEADERS = ("multiplier", "ratio", "倍率", "费率")
PRICE_RE = re.compile(r"(?<![\d.])\d[\d,]*(?:\.\d+)?")


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _header(value: Any) -> str:
    return _clean(value).lower().replace("_", " ").replace("-", " ")


def _matches(header: str, candidates: Iterable[str]) -> bool:
    return any(candidate in header for candidate in candidates)


def _price(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return format(Decimal(str(value)).normalize(), "f")
        except InvalidOperation:
            return None
    match = PRICE_RE.search(str(value))
    if not match:
        return None
    try:
        return format(Decimal(match.group().replace(",", "")).normalize(), "f")
    except InvalidOperation:
        return None


def _currency(value: str) -> str | None:
    upper = value.upper()
    if "$" in value or "USD" in upper or "美元" in value:
        return "USD"
    if "¥" in value or "￥" in value or "CNY" in upper or "RMB" in upper or "人民币" in value:
        return "CNY"
    return None


def _price_unit(value: str) -> str | None:
    lower = value.lower().replace(" ", "")
    if "1m" in lower or "million" in lower or "百万" in value or "/m" in lower:
        return "per_1m_tokens"
    if "1k" in lower or "/k" in lower or "千" in value:
        return "per_1k_tokens"
    return None


def _empty_model(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "input_price": None,
        "output_price": None,
        "group": None,
        "multiplier": None,
        "currency": None,
        "price_unit": None,
    }


def _extract_tables(soup: BeautifulSoup) -> dict[str, dict[str, Any]]:
    models: dict[str, dict[str, Any]] = {}
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        header_cells = rows[0].find_all(["th", "td"])
        headers = [_header(cell.get_text(" ", strip=True)) for cell in header_cells]
        model_index = next(
            (i for i, value in enumerate(headers) if _matches(value, MODEL_HEADERS)), None
        )
        if model_index is None:
            continue
        input_index = next(
            (i for i, value in enumerate(headers) if _matches(value, INPUT_HEADERS)), None
        )
        output_index = next(
            (i for i, value in enumerate(headers) if _matches(value, OUTPUT_HEADERS)), None
        )
        group_index = next(
            (i for i, value in enumerate(headers) if _matches(value, GROUP_HEADERS)), None
        )
        multiplier_index = next(
            (i for i, value in enumerate(headers) if _matches(value, MULTIPLIER_HEADERS)), None
        )
        for row in rows[1:]:
            cells = [_clean(cell.get_text(" ", strip=True)) for cell in row.find_all(["td", "th"])]
            if len(cells) <= model_index:
                continue
            name = cells[model_index]
            if not name or _header(name) in MODEL_HEADERS:
                continue
            model = _empty_model(name)
            for index, field in (
                (input_index, "input_price"),
                (output_index, "output_price"),
            ):
                if index is None or len(cells) <= index:
                    continue
                raw = cells[index]
                model[field] = _price(raw)
                model["currency"] = model["currency"] or _currency(raw) or _currency(headers[index])
                model["price_unit"] = model["price_unit"] or _price_unit(raw) or _price_unit(headers[index])
            if group_index is not None and len(cells) > group_index:
                model["group"] = cells[group_index] or None
            if multiplier_index is not None and len(cells) > multiplier_index:
                model["multiplier"] = cells[multiplier_index] or None
            models[name] = model
    return models


def _walk_json(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _first(item: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in item and item[key] not in (None, ""):
            return item[key]
    return None


def _extract_embedded_models(soup: BeautifulSoup) -> dict[str, dict[str, Any]]:
    models: dict[str, dict[str, Any]] = {}
    for script in soup.find_all("script"):
        content = script.string or script.get_text()
        content = content.strip()
        if not content or content[0] not in "[{":
            continue
        try:
            payload = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            continue
        for item in _walk_json(payload):
            pricing = item.get("pricing") if isinstance(item.get("pricing"), dict) else {}
            source = {**item, **pricing}
            name = _first(item, ("model", "model_id", "model_name", "slug", "id"))
            input_value = _first(
                source,
                ("input_price", "inputPrice", "prompt_price", "promptPrice", "prompt", "input"),
            )
            output_value = _first(
                source,
                ("output_price", "outputPrice", "completion_price", "completionPrice", "completion", "output"),
            )
            if not name or (input_value is None and output_value is None):
                continue
            name = _clean(name)
            if not name or len(name) > 300:
                continue
            model = models.setdefault(name, _empty_model(name))
            model["input_price"] = _price(input_value)
            model["output_price"] = _price(output_value)
            model["group"] = _clean(_first(source, ("group", "group_name"))) or None
            model["multiplier"] = _clean(_first(source, ("multiplier", "ratio", "rate"))) or None
            model["currency"] = _clean(_first(source, ("currency", "currency_code"))) or None
    return models


def _extract_metadata(soup: BeautifulSoup) -> dict[str, Any]:
    billing_methods: list[str] = []
    exchange_rates: list[str] = []
    minimum_recharges: list[str] = []
    discounts: list[str] = []
    for raw_line in soup.get_text("\n", strip=True).splitlines():
        line = _clean(raw_line)
        if not line or len(line) > 500:
            continue
        lower = line.lower()
        if "计费方式" in line or "按量计费" in line or "按次计费" in line or "billing method" in lower:
            billing_methods.append(line)
        if "汇率" in line or "exchange rate" in lower:
            exchange_rates.append(line)
        if "最低充值" in line or "最低儲值" in line or "minimum recharge" in lower:
            minimum_recharges.append(line)
        if any(word in lower for word in ("赠金", "折扣", "充值送", "返利", "discount", "bonus")):
            discounts.append(line)
    return {
        "billing_method": sorted(set(billing_methods)),
        "exchange_rate": sorted(set(exchange_rates)),
        "minimum_recharge": sorted(set(minimum_recharges)),
        "discount_rules": sorted(set(discounts)),
    }


def _extract_cards(soup: BeautifulSoup) -> dict[str, dict[str, Any]]:
    """提取 li/article 卡片中“$1/M input tokens”形式的价格。"""
    models: dict[str, dict[str, Any]] = {}
    price_template = (
        r"(?P<value>[$¥￥]?\s*\d[\d,]*(?:\.\d+)?)\s*"
        r"(?:/\s*(?P<unit>1?[MK]))?\s*(?:tokens?\s*)?{label}\b"
    )
    input_re = re.compile(price_template.format(label="input"), re.IGNORECASE)
    output_re = re.compile(price_template.format(label="output"), re.IGNORECASE)
    for card in soup.find_all(["li", "article"]):
        if card.find(["li", "article"]):
            continue
        anchor = card.find("a", attrs={"aria-label": True})
        name = _clean(anchor.get("aria-label")) if anchor else ""
        if not name or len(name) > 300:
            continue
        text = _clean(card.get_text(" ", strip=True))
        input_match = input_re.search(text)
        output_match = output_re.search(text)
        if not input_match and not output_match:
            continue
        model = _empty_model(name)
        for match, field in ((input_match, "input_price"), (output_match, "output_price")):
            if not match:
                continue
            raw = match.group("value")
            unit = match.group("unit")
            model[field] = _price(raw)
            model["currency"] = model["currency"] or _currency(raw)
            model["price_unit"] = model["price_unit"] or (_price_unit(f"/{unit}") if unit else None)
        models[name] = model
    return models


def extract_pricing(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    models = _extract_embedded_models(soup)
    models.update(_extract_cards(soup))
    models.update(_extract_tables(soup))
    metadata = _extract_metadata(soup)
    parse_ok = bool(models or any(metadata.values()))
    warnings = [] if parse_ok else ["未识别到模型价格或定价口径，请检查站点结构"]
    return {"models": models, **metadata, "parse_ok": parse_ok, "warnings": warnings}


def extract_metadata_only(html: str) -> dict[str, Any]:
    metadata = _extract_metadata(BeautifulSoup(html, "html.parser"))
    parse_ok = any(metadata.values())
    return {
        "models": {},
        **metadata,
        "parse_ok": parse_ok,
        "validation_pending": False,
        "warnings": [] if parse_ok else ["未识别到计费或充值口径文本"],
    }


def extract_models(payload: str) -> dict[str, Any]:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        return {"models": [], "parse_ok": False, "warnings": [f"模型接口不是有效 JSON: {exc.msg}"]}

    candidates: list[Any] | None = None
    if isinstance(data, list):
        candidates = data
    elif isinstance(data, dict):
        for key in ("data", "models", "items", "results"):
            if isinstance(data.get(key), list):
                candidates = data[key]
                break
    if candidates is None:
        return {"models": [], "parse_ok": False, "warnings": ["未找到模型清单数组"]}

    names: set[str] = set()
    for item in candidates:
        if isinstance(item, str):
            name = item
        elif isinstance(item, dict):
            name = _first(item, ("id", "model", "model_id", "model_name", "name"))
        else:
            continue
        name = _clean(name)
        if name:
            names.add(name)
    return {
        "models": sorted(names),
        "parse_ok": bool(names),
        "warnings": [] if names else ["模型清单为空或条目缺少模型 ID"],
    }
