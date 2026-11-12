from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..models import Snapshot, Target
from ..services.changes import diff_models, diff_pricing, previous_baseline
from ..services.migration import compare_extractions, record_dual_mismatch
from ..time_utils import utc_now
from .extract import extract_metadata_only, extract_models, extract_pricing
from .http import FetchError, Fetcher
from .llm import LLMExtractor, clean_html_for_llm

logger = logging.getLogger(__name__)


class MissingAuthError(RuntimeError):
    pass


class ExtractionError(RuntimeError):
    def __init__(self, message: str, *, raw_content: str, source_url: str) -> None:
        super().__init__(message)
        self.raw_content = raw_content
        self.source_url = source_url


@dataclass
class CollectStats:
    success: int = 0
    failed: int = 0
    skipped: int = 0
    changes: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "success": self.success,
            "failed": self.failed,
            "skipped": self.skipped,
            "changes": self.changes,
        }


class SiteCollector:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        fetcher: Fetcher | None = None,
        llm_extractor: LLMExtractor | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.fetcher = fetcher or Fetcher()
        self._llm_extractor = llm_extractor

    @property
    def llm_extractor(self) -> LLMExtractor:
        if self._llm_extractor is None:
            self._llm_extractor = LLMExtractor()
        return self._llm_extractor

    def collect_all(self, groups: tuple[str, ...] = ("A", "B")) -> dict[str, int]:
        stats = CollectStats()
        with self.session_factory() as session:
            target_ids = session.scalars(
                select(Target.id)
                .where(Target.enabled.is_(True), Target.group_code.in_(groups))
                .order_by(Target.id)
            ).all()
        for target_id in target_ids:
            with self.session_factory() as session:
                target = session.get(Target, target_id)
                if target is None or not target.enabled:
                    continue
                for kind, url in (("pricing", target.pricing_url), ("models", target.models_url)):
                    if not url:
                        stats.skipped += 1
                        continue
                    try:
                        created = self._collect_one(session, target, kind, url)
                        stats.success += 1
                        stats.changes += created
                    except Exception as exc:
                        session.rollback()
                        logger.exception("采集失败 target=%s kind=%s url=%s", target.name, kind, url)
                        failure = Snapshot(
                            target_id=target.id,
                            kind=kind,
                            source_url=getattr(exc, "source_url", url),
                            raw_content=getattr(exc, "raw_content", None),
                            collected_at=utc_now(),
                            status="failed",
                            parse_ok=False,
                            error=f"{type(exc).__name__}: {exc}"[:8000],
                        )
                        session.add(failure)
                        session.commit()
                        stats.failed += 1
        return stats.as_dict()

    def _auth_headers(self, target: Target) -> dict[str, str] | None:
        if target.auth_type == "none":
            return None
        if not target.auth_env_key:
            raise MissingAuthError("已启用认证但未配置 auth_env_key")
        secret = os.getenv(target.auth_env_key)
        if not secret:
            raise MissingAuthError(f"环境变量 {target.auth_env_key} 未配置，改用公开页面")
        if target.auth_type == "bearer":
            return {"Authorization": f"Bearer {secret}"}
        if target.auth_type == "custom_header":
            return {target.auth_header_name or "X-API-Key": secret}
        raise ValueError(f"不支持的认证方式: {target.auth_type}")

    def _collect_one(self, session: Session, target: Target, kind: str, url: str) -> int:
        try:
            headers = self._auth_headers(target) if kind == "models" else None
            result = self.fetcher.fetch(
                url,
                method=target.collect_method if kind == "pricing" else "http",
                headers=headers,
            )
        except (MissingAuthError, FetchError) as exc:
            can_fallback = (
                kind == "models"
                and target.models_fallback_url
                and (
                    isinstance(exc, MissingAuthError)
                    or exc.status_code in {401, 403}
                )
            )
            if not can_fallback:
                raise
            logger.warning("模型 API 不可用，改用公开页面 target=%s reason=%s", target.name, exc)
            return self._collect_models_webpage(session, target, str(exc))

        try:
            extracted, parser_extracted, comparison = self._extract(target, kind, result.text)
        except Exception as exc:
            raise ExtractionError(
                f"{type(exc).__name__}: {exc}", raw_content=result.text, source_url=result.url
            ) from exc
        return self._store_snapshot(
            session, target, kind, result.url, result.text, extracted,
            parser_extracted=parser_extracted, comparison=comparison,
        )

    def _parser_extract(self, target: Target, raw: str) -> dict:
        if target.pricing_mode == "metadata_only":
            return extract_metadata_only(raw)
        return extract_pricing(raw)

    def _extract(self, target: Target, kind: str, raw: str) -> tuple[dict, dict | None, dict | None]:
        if kind == "models":
            return extract_models(raw), None, None
        method = target.extraction_method
        if method == "llm":
            return self.llm_extractor.extract(clean_html_for_llm(raw)), None, None
        parser_result = self._parser_extract(target, raw)
        if method == "parser":
            return parser_result, None, None
        if method != "dual":
            raise ValueError(f"不支持的提取方式: {method}")
        llm_result = self.llm_extractor.extract(clean_html_for_llm(raw))
        comparison = compare_extractions(parser_result, llm_result)
        return llm_result, parser_result, comparison

    def _collect_models_webpage(
        self, session: Session, target: Target, api_error: str
    ) -> int:
        session.add(Snapshot(
            target_id=target.id,
            kind="models",
            source_url=target.models_url,
            collected_at=utc_now(),
            status="failed",
            parse_ok=False,
            error=f"API 失败，已尝试公开页面兜底: {api_error}"[:8000],
        ))
        session.flush()
        result = self.fetcher.fetch(target.models_fallback_url or "", method=target.collect_method)
        try:
            llm_result = self.llm_extractor.extract(clean_html_for_llm(result.text))
        except Exception as exc:
            raise ExtractionError(
                f"公开页面提取失败: {type(exc).__name__}: {exc}",
                raw_content=result.text,
                source_url=result.url,
            ) from exc
        names = sorted({
            item["name"] for item in llm_result.get("models", [])
            if isinstance(item, dict) and item.get("name")
        })
        warnings = list(llm_result.get("warnings", []))
        if not names:
            warnings.append("公开模型页面未提取到模型名")
        extracted = {
            "models": names,
            "parse_ok": bool(names),
            "validation_pending": bool(llm_result.get("validation_pending")),
            "invalid_fields": llm_result.get("invalid_fields", []),
            "warnings": warnings,
            "llm_usage": llm_result.get("llm_usage", {}),
        }
        return self._store_snapshot(
            session, target, "models_webpage", result.url, result.text, extracted
        )

    def _store_snapshot(
        self,
        session: Session,
        target: Target,
        kind: str,
        source_url: str,
        raw: str,
        extracted: dict,
        *,
        parser_extracted: dict | None = None,
        comparison: dict | None = None,
    ) -> int:
        dual_successful = None
        dual_match = None
        differences = None
        if comparison is not None:
            dual_successful = bool(parser_extracted.get("parse_ok") and extracted.get("parse_ok"))
            if not dual_successful and not comparison["differences"]:
                comparison["differences"].append({
                    "field": "parse_ok",
                    "parser": bool(parser_extracted.get("parse_ok")),
                    "llm": bool(extracted.get("parse_ok")),
                })
            dual_match = bool(comparison["match"] and dual_successful)
            comparison["match"] = dual_match
            differences = comparison["differences"]
        usage = extracted.get("llm_usage") or {}
        estimated_tokens = usage.get("estimated_total_tokens")
        if estimated_tokens is None:
            estimated_tokens = usage.get("estimated_input_tokens", 0) + usage.get("estimated_output_tokens", 0)
        warnings = list(extracted.get("warnings", []))
        if dual_match is False:
            warnings.append(f"双跑存在 {len(differences or [])} 项差异，以 LLM 结果为主并待核对")
        snapshot = Snapshot(
            target_id=target.id,
            kind=kind,
            source_url=source_url,
            raw_content=raw,
            extracted_json=extracted,
            parser_extracted_json=parser_extracted,
            dual_difference_fields=differences,
            dual_match=dual_match,
            dual_successful=dual_successful,
            llm_calls=usage.get("calls", 0),
            llm_estimated_tokens=estimated_tokens,
            llm_usage_json=usage or None,
            collected_at=utc_now(),
            status="success",
            parse_ok=bool(extracted.get("parse_ok")),
            error="；".join(warnings) or None,
        )
        session.add(snapshot)
        session.flush()
        created: list = []
        if snapshot.parse_ok:
            previous = previous_baseline(
                session, target_id=target.id, kind=kind, current_snapshot_id=snapshot.id
            )
            if previous is not None:
                if kind == "pricing":
                    created = diff_pricing(
                        session, target=target, previous=previous, current=snapshot
                    )
                elif kind in {"models", "models_webpage"}:
                    created = diff_models(
                        session, target=target, previous=previous, current=snapshot
                    )
        if comparison is not None and dual_match is False:
            created.append(record_dual_mismatch(
                session, target=target, snapshot=snapshot, comparison=comparison
            ))
        session.commit()
        logger.info(
            "采集完成 target=%s kind=%s parse_ok=%s changes=%d dual_match=%s",
            target.name, kind, snapshot.parse_ok, len(created), snapshot.dual_match,
        )
        return len(created)
