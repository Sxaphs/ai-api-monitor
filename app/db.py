from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import settings
from .models import Base


def make_engine(database_url: str) -> Engine:
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    return create_engine(database_url, connect_args=connect_args, future=True)


engine = make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


PATCH_COLUMNS = {
    "targets": {
        "models_fallback_url": "VARCHAR(1000)",
        "pricing_mode": "VARCHAR(20)",
        "price_data_missing": "BOOLEAN",
        "auth_type": "VARCHAR(20)",
        "auth_env_key": "VARCHAR(120)",
        "auth_header_name": "VARCHAR(120)",
        "extraction_method": "VARCHAR(20)",
        "dual_started_at": "DATETIME",
    },
    "snapshots": {
        "parser_extracted_json": "JSON",
        "dual_difference_fields": "JSON",
        "dual_match": "BOOLEAN",
        "dual_successful": "BOOLEAN",
        "dual_llm_confirmed": "BOOLEAN",
        "llm_calls": "INTEGER",
        "llm_estimated_tokens": "INTEGER",
        "llm_usage_json": "JSON",
    },
    "changes": {"data_source": "VARCHAR(20)"},
    "reports": {
        "title": "VARCHAR(200)",
        "report_kind": "VARCHAR(20)",
    },
}


def _ensure_patch_columns(bind: Engine) -> None:
    # 第一期补丁的轻量升级；表名、列名和类型均为上方固定常量。
    with bind.begin() as connection:
        for table, columns in PATCH_COLUMNS.items():
            existing = {column["name"] for column in inspect(connection).get_columns(table)}
            for name, ddl in columns.items():
                if name not in existing:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def init_db(bind: Engine | None = None) -> None:
    bind = bind or engine
    if bind.url.get_backend_name() == "sqlite" and bind.url.database not in {None, "", ":memory:"}:
        Path(bind.url.database).parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind)
    _ensure_patch_columns(bind)


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session
