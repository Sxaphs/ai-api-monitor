from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .time_utils import utc_now


class Base(DeclarativeBase):
    pass


class Target(Base):
    __tablename__ = "targets"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    group_code: Mapped[str] = mapped_column(String(1), index=True, nullable=False)
    website_url: Mapped[str | None] = mapped_column(String(1000))
    pricing_url: Mapped[str | None] = mapped_column(String(1000))
    models_url: Mapped[str | None] = mapped_column(String(1000))
    models_fallback_url: Mapped[str | None] = mapped_column(String(1000))
    changelog_url: Mapped[str | None] = mapped_column(String(1000))
    x_handle: Mapped[str | None] = mapped_column(String(120))
    collect_method: Mapped[str] = mapped_column(String(20), default="http", nullable=False)
    pricing_mode: Mapped[str] = mapped_column(String(20), default="auto", nullable=False)
    extraction_method: Mapped[str] = mapped_column(String(20), default="dual", nullable=False)
    dual_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    price_data_missing: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    auth_type: Mapped[str] = mapped_column(String(20), default="none", nullable=False)
    auth_env_key: Mapped[str | None] = mapped_column(String(120))
    auth_header_name: Mapped[str | None] = mapped_column(String(120), default="X-API-Key")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    x_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class Snapshot(Base):
    __tablename__ = "snapshots"
    __table_args__ = (
        UniqueConstraint(
            "target_id", "kind", "external_id", name="uq_snapshot_external_id"
        ),
        Index("ix_snapshot_baseline", "target_id", "kind", "status", "collected_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(1000))
    external_id: Mapped[str | None] = mapped_column(String(160))
    raw_content: Mapped[str | None] = mapped_column(Text)
    extracted_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON)
    parser_extracted_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    dual_difference_fields: Mapped[list[Any] | None] = mapped_column(JSON)
    dual_match: Mapped[bool | None] = mapped_column(Boolean)
    dual_successful: Mapped[bool | None] = mapped_column(Boolean)
    dual_llm_confirmed: Mapped[bool | None] = mapped_column(Boolean)
    llm_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    llm_estimated_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    llm_usage_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    parse_ok: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)


class Change(Base):
    __tablename__ = "changes"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_id", "change_type", "entity_key", name="uq_change_snapshot_entity"
        ),
        Index("ix_changes_detected_group", "detected_at", "signal_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id"), nullable=False)
    snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("snapshots.id"))
    change_type: Mapped[str] = mapped_column(String(30), index=True, nullable=False)
    entity_key: Mapped[str | None] = mapped_column(String(500))
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    change_percent: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    signal_type: Mapped[str] = mapped_column(String(20), index=True, nullable=False)
    severity: Mapped[str] = mapped_column(String(10), default="low", nullable=False)
    verify_status: Mapped[str] = mapped_column(
        String(20), default="pending", nullable=False, index=True
    )
    related_id: Mapped[int | None] = mapped_column(ForeignKey("changes.id"))
    source_url: Mapped[str | None] = mapped_column(String(1000))
    data_source: Mapped[str | None] = mapped_column(String(20))
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    extra_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    related = relationship("Change", remote_side=[id], uselist=False)


class Report(Base):
    __tablename__ = "reports"
    __table_args__ = (Index("ix_reports_period", "period_start", "period_end"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str | None] = mapped_column(String(200))
    report_kind: Mapped[str | None] = mapped_column(String(20))
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
