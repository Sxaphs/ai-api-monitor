from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Target
from .time_utils import utc_now

# 未能确认的采集 URL 一律留空，避免把猜测地址当成真实竞品站点。
SEED_TARGETS = [
    # A 组：正规军竞品
    dict(name="OpenRouter", group_code="A", website_url="https://openrouter.ai", pricing_url="https://openrouter.ai/models", models_url="https://openrouter.ai/api/v1/models", x_handle="@OpenRouter"),
    dict(name="302.AI", group_code="A", website_url="https://302.ai", pricing_url="https://302.ai/pricing", pricing_mode="metadata_only", price_data_missing=True),
    dict(name="AiHubMix", group_code="A", website_url="https://aihubmix.com", pricing_url="https://aihubmix.com/pricing", pricing_mode="llm", extraction_method="llm"),
    dict(name="硅基流动 SiliconFlow", group_code="A", website_url="https://siliconflow.cn", pricing_url="https://siliconflow.cn/pricing", models_url="https://api.siliconflow.cn/v1/models", models_fallback_url="https://www.siliconflow.cn/models", pricing_mode="llm", extraction_method="llm", auth_type="bearer", auth_env_key="SILICONFLOW_KEY"),
    dict(name="DMXAPI", group_code="A", website_url="https://www.dmxapi.cn"),
    dict(name="API2D", group_code="A", website_url="https://api2d.com", pricing_url="https://api2d.com/pricing", pricing_mode="llm", extraction_method="llm"),
    dict(name="词元无忧", group_code="A"),
    dict(name="CloseAI", group_code="A", website_url="https://closeai-asia.com"),
    # B 组：野路子竞品
    dict(name="PackyCode", group_code="B", website_url="https://www.packycode.com"),
    dict(name="Jeniya", group_code="B"),
    dict(name="aigocode", group_code="B", website_url="https://aigocode.com"),
    dict(name="云雾API", group_code="B", website_url="https://yunwu.ai"),
    dict(name="AICodeMirror", group_code="B", website_url="https://aicodemirror.com"),
    dict(name="OneXModel", group_code="B", website_url="https://onexmodel.com"),
    dict(name="Aiberm", group_code="B"),
    dict(name="AiYa", group_code="B"),
    # C 组：本期仅配置，不采集
    dict(name="RouterHubs", group_code="C", website_url="https://routerhubs.com"),
    dict(name="API选", group_code="C", website_url="https://apixuan.com"),
    dict(name="HowToken", group_code="C", website_url="https://howtok.net"),
    dict(name="EggStriker.AI", group_code="C", website_url="https://eggstriker.ai"),
    # D 组：本期只采 X；handle 由运营方手工补录
    dict(name="OpenAI", group_code="D", website_url="https://openai.com"),
    dict(name="Anthropic", group_code="D", website_url="https://www.anthropic.com"),
    dict(name="Google DeepMind", group_code="D", website_url="https://deepmind.google"),
    dict(name="DeepSeek", group_code="D", website_url="https://www.deepseek.com"),
    dict(name="阿里云", group_code="D", website_url="https://www.aliyun.com"),
    dict(name="火山引擎", group_code="D", website_url="https://www.volcengine.com"),
]


PATCH_DEFAULTS = {
    "302.AI": {"pricing_mode": "metadata_only", "price_data_missing": True},
    "AiHubMix": {"pricing_mode": "llm"},
    "API2D": {"pricing_mode": "llm"},
    "硅基流动 SiliconFlow": {
        "pricing_mode": "llm",
        "auth_type": "bearer",
        "auth_env_key": "SILICONFLOW_KEY",
        "models_fallback_url": "https://www.siliconflow.cn/models",
    },
}


def seed_targets(session: Session) -> int:
    existing = {target.name: target for target in session.scalars(select(Target)).all()}
    created = 0
    for values in SEED_TARGETS:
        if values["name"] in existing:
            continue
        target = Target(**values)
        if target.x_handle:
            target.x_since = utc_now()
        session.add(target)
        created += 1

    # 老库新增列先为 NULL，只在对应字段第一次升级时应用默认值。
    for name, target in existing.items():
        if target.extraction_method is None:
            target.extraction_method = "llm" if name in {"AiHubMix", "硅基流动 SiliconFlow", "API2D"} else "dual"
            target.dual_started_at = utc_now() if target.extraction_method == "dual" else None
        if target.pricing_mode is not None:
            continue
        target.pricing_mode = "auto"
        target.auth_type = "none"
        target.auth_header_name = "X-API-Key"
        target.price_data_missing = False
        for field, value in PATCH_DEFAULTS.get(name, {}).items():
            setattr(target, field, value)
    session.commit()
    return created
