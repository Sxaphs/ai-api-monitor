from __future__ import annotations

from datetime import date, datetime, time, timedelta
from html import escape
from pathlib import Path
from urllib.parse import urlparse

import markdown
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import get_db
from .models import Change, Report, Snapshot, Target
from .schemas import TargetCreate, TargetUpdate
from .services.migration import llm_usage_by_day
from .services.reports import ReportService, recent_report_window
from .time_utils import BEIJING, ensure_utc, utc_now

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["bj"] = lambda value: ensure_utc(value).astimezone(BEIJING).strftime("%Y-%m-%d %H:%M")
CHANGE_TYPES = ("涨价", "降价", "新模型", "模型下线", "新功能", "营销活动", "口径变化", "X公告", "价格变动", "提取分歧")
URL_FIELDS = {"website_url", "pricing_url", "models_url", "models_fallback_url", "changelog_url"}


def _validate_urls(values: dict) -> None:
    for field in URL_FIELDS:
        value = values.get(field)
        if value is not None and urlparse(value).scheme not in {"http", "https"}:
            raise HTTPException(status_code=422, detail=f"{field} 必须是 http(s) URL")


@router.get("/", include_in_schema=False)
def index():
    return RedirectResponse("/changes", status_code=303)


@router.get("/changes", include_in_schema=False)
def changes_page(
    request: Request,
    group: str | None = None,
    change_type: str | None = None,
    signal_type: str | None = None,
    session: Session = Depends(get_db),
):
    query = select(Change, Target).join(Target, Change.target_id == Target.id)
    if group in {"A", "B", "C", "D"}:
        query = query.where(Target.group_code == group)
    else:
        group = None
    if change_type in CHANGE_TYPES:
        query = query.where(Change.change_type == change_type)
    else:
        change_type = None
    if signal_type in {"confirmed", "warning"}:
        query = query.where(Change.signal_type == signal_type)
    else:
        signal_type = None
    rows = session.execute(query.order_by(Change.detected_at.desc(), Change.id.desc()).limit(1000)).all()
    return templates.TemplateResponse(
        request=request,
        name="changes.html",
        context={
            "rows": rows,
            "group": group,
            "change_type": change_type,
            "signal_type": signal_type,
            "change_types": CHANGE_TYPES,
            "group_labels": GROUP_LABELS,
            "llm_usage": llm_usage_by_day(session),
        },
    )


@router.get("/review", include_in_schema=False)
def review_page(request: Request, session: Session = Depends(get_db)):
    rows = session.execute(
        select(Change, Target)
        .join(Target, Change.target_id == Target.id)
        .where(Change.verify_status == "pending")
        .order_by(Change.detected_at.desc(), Change.id.desc())
    ).all()
    return templates.TemplateResponse(
        request=request,
        name="review.html",
        context={"rows": rows, "group_labels": GROUP_LABELS},
    )


def _set_verify_status(change_id: int, verify_status: str, session: Session) -> RedirectResponse:
    change = session.get(Change, change_id)
    if change is None:
        raise HTTPException(status_code=404, detail="变动记录不存在")
    change.verify_status = verify_status
    change.verified_at = utc_now()
    if change.snapshot_id:
        snapshot = session.get(Snapshot, change.snapshot_id)
        if snapshot and snapshot.dual_match is False:
            snapshot.dual_llm_confirmed = verify_status == "confirmed"
    session.commit()
    return RedirectResponse("/review", status_code=303)


@router.post("/review/{change_id}/confirm", include_in_schema=False)
def confirm_change(change_id: int, session: Session = Depends(get_db)):
    return _set_verify_status(change_id, "confirmed", session)


@router.post("/review/{change_id}/false-alarm", include_in_schema=False)
def reject_change(change_id: int, session: Session = Depends(get_db)):
    return _set_verify_status(change_id, "false_alarm", session)


GROUP_LABELS = {
    "A": "正规军竞品",
    "B": "野路子竞品",
    "C": "第三方监控/榜单平台",
    "D": "上游供应商",
}
REPORT_KIND_LABELS = {"weekly": "周报", "monthly": "月报", "custom": "自定义"}


def _reports_page(
    request: Request,
    session: Session,
    *,
    error: str | None = None,
    status_code: int = 200,
):
    reports = session.scalars(
        select(Report).order_by(Report.generated_at.desc(), Report.id.desc()).limit(100)
    ).all()
    return templates.TemplateResponse(
        request=request,
        name="reports.html",
        context={
            "reports": reports,
            "error": error,
            "kind_labels": REPORT_KIND_LABELS,
        },
        status_code=status_code,
    )


@router.get("/reports", include_in_schema=False)
def reports_page(request: Request, session: Session = Depends(get_db)):
    return _reports_page(request, session)


@router.post("/reports", include_in_schema=False)
async def generate_report(request: Request, session: Session = Depends(get_db)):
    form = await request.form()
    preset = str(form.get("preset") or "custom")
    try:
        if preset in {"7", "30"}:
            days = int(preset)
            start, end = recent_report_window(days)
            period_name = f"近 {days} 天"
            title = f"AI API 中转站竞品监测{'周报' if days == 7 else '月报'}（{period_name}）"
            report_kind = "weekly" if days == 7 else "monthly"
        elif preset == "custom":
            start_date = date.fromisoformat(str(form.get("start_date") or ""))
            end_date = date.fromisoformat(str(form.get("end_date") or ""))
            if start_date > end_date:
                raise ValueError("开始日期不能晚于结束日期")
            start = datetime.combine(start_date, time.min, tzinfo=BEIJING).astimezone(ensure_utc(utc_now()).tzinfo)
            end = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=BEIJING).astimezone(ensure_utc(utc_now()).tzinfo)
            title = f"AI API 中转站竞品监测自定义报告（{start_date} 至 {end_date}）"
            report_kind = "custom"
        else:
            raise ValueError("未知的报告时间范围")
        report = ReportService(session).generate(
            start, end, title=title, report_kind=report_kind
        )
    except ValueError as exc:
        message = "请输入有效的起止日期" if isinstance(exc, ValueError) and not str(exc) else str(exc)
        return _reports_page(request, session, error=message or str(exc), status_code=422)
    return RedirectResponse(f"/reports/{report.id}", status_code=303)


@router.get("/reports/{report_id}", include_in_schema=False)
def report_detail(report_id: int, request: Request, session: Session = Depends(get_db)):
    report = session.get(Report, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="报告不存在")
    rendered_content = markdown.markdown(escape(report.content), extensions=["tables"])
    return templates.TemplateResponse(
        request=request,
        name="report_detail.html",
        context={"report": report, "rendered_content": rendered_content},
    )


@router.get("/api/targets")
def list_targets(
    group: str | None = None,
    enabled: bool | None = None,
    session: Session = Depends(get_db),
):
    query = select(Target).order_by(Target.group_code, Target.id)
    if group in {"A", "B", "C", "D"}:
        query = query.where(Target.group_code == group)
    if enabled is not None:
        query = query.where(Target.enabled.is_(enabled))
    targets = session.scalars(query).all()
    return [_target_dict(target) for target in targets]


def _target_dict(target: Target) -> dict:
    return {
        "id": target.id,
        "name": target.name,
        "group_code": target.group_code,
        "website_url": target.website_url,
        "pricing_url": target.pricing_url,
        "models_url": target.models_url,
        "models_fallback_url": target.models_fallback_url,
        "changelog_url": target.changelog_url,
        "x_handle": target.x_handle,
        "collect_method": target.collect_method,
        "pricing_mode": target.pricing_mode,
        "extraction_method": target.extraction_method,
        "dual_started_at": target.dual_started_at.isoformat() if target.dual_started_at else None,
        "price_data_missing": target.price_data_missing,
        "auth_type": target.auth_type,
        "auth_env_key": target.auth_env_key,
        "auth_header_name": target.auth_header_name,
        "enabled": target.enabled,
        "x_since": target.x_since.isoformat() if target.x_since else None,
    }


@router.post("/api/targets", status_code=status.HTTP_201_CREATED)
def create_target(payload: TargetCreate, session: Session = Depends(get_db)):
    values = payload.model_dump()
    _validate_urls(values)
    target = Target(**values)
    if target.extraction_method == "dual":
        target.dual_started_at = utc_now()
    if target.x_handle and target.enabled:
        target.x_since = utc_now()
    session.add(target)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="同名监测对象已存在") from exc
    return _target_dict(target)


@router.patch("/api/targets/{target_id}")
def update_target(target_id: int, payload: TargetUpdate, session: Session = Depends(get_db)):
    target = session.get(Target, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="监测对象不存在")
    values = payload.model_dump(exclude_unset=True)
    _validate_urls(values)
    if "name" in values and values["name"] is None:
        raise HTTPException(status_code=422, detail="名称不能为空")
    old_handle = target.x_handle
    was_enabled = target.enabled
    old_method = target.extraction_method
    for field, value in values.items():
        setattr(target, field, value)
    if target.extraction_method == "dual" and (old_method != "dual" or target.dual_started_at is None):
        target.dual_started_at = utc_now()
    if target.x_handle and (
        target.x_handle != old_handle or (target.enabled and not was_enabled)
    ):
        target.x_since = utc_now()
    if not target.x_handle:
        target.x_since = None
    target.updated_at = utc_now()
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="同名监测对象已存在") from exc
    return _target_dict(target)


@router.post("/api/targets/{target_id}/toggle")
def toggle_target(target_id: int, session: Session = Depends(get_db)):
    target = session.get(Target, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="监测对象不存在")
    target.enabled = not target.enabled
    if target.enabled and target.x_handle:
        target.x_since = utc_now()
    target.updated_at = utc_now()
    session.commit()
    return _target_dict(target)


@router.delete("/api/targets/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_target(target_id: int, session: Session = Depends(get_db)):
    target = session.get(Target, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="监测对象不存在")
    change_ids = select(Change.id).where(Change.target_id == target_id)
    session.execute(update(Change).where(Change.related_id.in_(change_ids)).values(related_id=None))
    session.execute(delete(Change).where(Change.target_id == target_id))
    session.execute(delete(Snapshot).where(Snapshot.target_id == target_id))
    session.delete(target)
    session.commit()
