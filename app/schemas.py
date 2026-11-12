from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TargetCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    group_code: Literal["A", "B", "C", "D"]
    website_url: str | None = None
    pricing_url: str | None = None
    models_url: str | None = None
    models_fallback_url: str | None = None
    changelog_url: str | None = None
    x_handle: str | None = None
    collect_method: Literal["http", "playwright"] = "http"
    pricing_mode: Literal["auto", "llm", "metadata_only"] = "auto"
    extraction_method: Literal["parser", "llm", "dual"] = "dual"
    price_data_missing: bool = False
    auth_type: Literal["none", "bearer", "custom_header"] = "none"
    auth_env_key: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    auth_header_name: str | None = "X-API-Key"
    enabled: bool = True

    @field_validator(
        "name", "website_url", "pricing_url", "models_url", "models_fallback_url",
        "changelog_url", "x_handle", "auth_env_key", "auth_header_name", mode="before",
    )
    @classmethod
    def strip_blank(cls, value):
        if isinstance(value, str):
            return value.strip() or None
        return value


class TargetUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    group_code: Literal["A", "B", "C", "D"] | None = None
    website_url: str | None = None
    pricing_url: str | None = None
    models_url: str | None = None
    models_fallback_url: str | None = None
    changelog_url: str | None = None
    x_handle: str | None = None
    collect_method: Literal["http", "playwright"] | None = None
    pricing_mode: Literal["auto", "llm", "metadata_only"] | None = None
    extraction_method: Literal["parser", "llm", "dual"] | None = None
    price_data_missing: bool | None = None
    auth_type: Literal["none", "bearer", "custom_header"] | None = None
    auth_env_key: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    auth_header_name: str | None = None
    enabled: bool | None = None

    @field_validator(
        "name", "website_url", "pricing_url", "models_url", "models_fallback_url",
        "changelog_url", "x_handle", "auth_env_key", "auth_header_name", mode="before",
    )
    @classmethod
    def strip_blank(cls, value):
        if isinstance(value, str):
            return value.strip() or None
        return value
