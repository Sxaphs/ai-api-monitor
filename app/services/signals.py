from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

FEATURE_WORDS = ("launch", "support", "now available", "上线", "支持")
PRICE_WORDS = ("pricing", "price", "降价", "涨价", "discount")
MARKETING_WORDS = ("credit", "bonus", "赠金", "返利", "giveaway", "充值送")
WARNING_WORDS = (
    "coming soon",
    "soon",
    "即将",
    "预告",
    "下周",
    "waitlist",
    "preview",
    "beta",
    "deprecat",
    "retiring",
    "eol",
    "停售",
    "下线",
)
RETIREMENT_WORDS = ("deprecat", "retiring", "eol", "停售", "下线")


@dataclass(frozen=True)
class Signal:
    change_type: str
    signal_type: str
    severity: str
    matched_warnings: list[str] = field(default_factory=list)
    old_value: str | None = None
    new_value: str | None = None
    change_percent: Decimal | None = None


def _contains_any(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def _price_pair(text: str) -> tuple[str, str, Decimal] | None:
    pattern = re.compile(
        r"(?:from\s+|从\s*)?[$¥￥]?\s*(\d+(?:\.\d+)?)\s*"
        r"(?:->|→|to|降至|降到|涨至|涨到|调整为)\s*[$¥￥]?\s*(\d+(?:\.\d+)?)",
        re.IGNORECASE,
    )
    match = pattern.search(text)
    if not match:
        return None
    try:
        old = Decimal(match.group(1))
        new = Decimal(match.group(2))
    except InvalidOperation:
        return None
    if old == 0:
        return match.group(1), match.group(2), Decimal(0)
    percent = ((new - old) / old * Decimal(100)).quantize(Decimal("0.0001"))
    return match.group(1), match.group(2), percent


def classify_tweet(text: str, group_code: str) -> Signal:
    lower = text.lower()
    matched_warnings = [word for word in WARNING_WORDS if word in lower]
    is_retirement = _contains_any(lower, RETIREMENT_WORDS)
    is_marketing = _contains_any(lower, MARKETING_WORDS)
    is_price = _contains_any(lower, PRICE_WORDS)
    is_feature = _contains_any(lower, FEATURE_WORDS) or "new model" in lower or "新模型" in text

    if is_retirement:
        change_type = "模型下线"
    elif is_marketing:
        change_type = "营销活动"
    elif is_price:
        change_type = "价格变动"
    elif is_feature:
        change_type = "新模型" if "model" in lower or "模型" in text else "新功能"
    else:
        change_type = "X公告"

    upstream_landed_signal = group_code == "D" and change_type in {
        "价格变动",
        "新模型",
        "新功能",
        "模型下线",
    }
    is_warning = bool(matched_warnings) or upstream_landed_signal
    if is_warning:
        if group_code == "D" and change_type in {"价格变动", "模型下线"}:
            severity = "high"
        elif change_type == "模型下线":
            severity = "high"
        elif change_type in {"新模型", "新功能"}:
            severity = "medium"
        else:
            severity = "low"
    else:
        severity = "medium" if change_type != "X公告" else "low"

    pair = _price_pair(text) if change_type == "价格变动" else None
    return Signal(
        change_type=change_type,
        signal_type="warning" if is_warning else "confirmed",
        severity=severity,
        matched_warnings=matched_warnings,
        old_value=pair[0] if pair else None,
        new_value=pair[1] if pair else None,
        change_percent=pair[2] if pair else None,
    )


def is_original_tweet(tweet: dict[str, Any]) -> bool:
    text = str(tweet.get("text") or tweet.get("full_text") or "")
    if text.lstrip().lower().startswith("rt @"):
        return False
    if tweet.get("retweeted") is True or tweet.get("retweeted_status"):
        return False
    reply_fields = (
        "in_reply_to_status_id",
        "in_reply_to_status_id_str",
        "in_reply_to_screen_name",
        "reply_to_tweet_id",
    )
    if any(tweet.get(field) not in (None, "", False) for field in reply_fields):
        return False
    return not tweet.get("is_reply", False)
