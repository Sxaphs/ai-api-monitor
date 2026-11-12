from __future__ import annotations

from html import escape
from pathlib import Path

import markdown
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db import SessionLocal
from app.models import Change, Report, Target
from app.time_utils import BEIJING, ensure_utc, utc_now

GROUP_LABELS = {
    "A": "正规军竞品",
    "B": "野路子竞品",
    "C": "第三方监控/榜单平台",
    "D": "上游供应商",
}
STYLE = """
body{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;max-width:1280px;margin:auto;padding:24px;color:#172033;background:#f6f8fb;line-height:1.65}
nav{display:flex;gap:18px;margin-bottom:24px}a{color:#175cd3}table{width:100%;border-collapse:collapse;background:#fff}th,td{padding:10px;border:1px solid #dfe5ee;text-align:left;vertical-align:top}th{background:#edf2f7}.card,.report{background:#fff;padding:24px;border:1px solid #dfe5ee;border-radius:10px}.muted{color:#667085}.report{overflow-x:auto}.report blockquote{border-left:3px solid #f79009;margin-left:0;padding-left:14px}.badge{font-size:12px;background:#e8eef7;border-radius:4px;padding:2px 6px}code{word-break:break-all}
"""


def _local(value) -> str:
    return ensure_utc(value).astimezone(BEIJING).strftime("%Y-%m-%d %H:%M")


def _layout(title: str, body: str, *, nested: bool = False) -> str:
    prefix = "../" if nested else ""
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)}</title><style>{STYLE}</style></head><body>
<nav><a href="{prefix}index.html">报告列表</a><a href="{prefix}changes.html">变动记录</a></nav>
{body}
<p class="muted">这是 GitHub Pages 静态只读镜像；在线生成、采集和人工核对请在 FastAPI 系统中操作。</p>
</body></html>"""


def _safe_url(url: str | None) -> str | None:
    if url and url.startswith(("https://", "http://")):
        return escape(url, quote=True)
    return None


def export_site(output_dir: str | Path, session_factory: sessionmaker[Session] = SessionLocal) -> dict[str, int]:
    output = Path(output_dir)
    reports_dir = output / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    with session_factory() as session:
        reports = session.scalars(
            select(Report).order_by(Report.generated_at.desc(), Report.id.desc())
        ).all()
        changes = session.execute(
            select(Change, Target)
            .join(Target, Change.target_id == Target.id)
            .order_by(Change.detected_at.desc(), Change.id.desc())
            .limit(1000)
        ).all()

        report_rows = []
        for report in reports:
            title = report.title or f"历史报告 {report.id}"
            report_rows.append(
                f"<tr><td>{_local(report.generated_at)}</td>"
                f"<td><a href=\"reports/{report.id}.html\">{escape(title)}</a></td>"
                f"<td>{_local(report.period_start)} 至 {_local(report.period_end)}</td></tr>"
            )
            rendered = markdown.markdown(escape(report.content), extensions=["tables"])
            detail = f"<article class=\"report\">{rendered}</article>"
            (reports_dir / f"{report.id}.html").write_text(
                _layout(title, detail, nested=True), encoding="utf-8"
            )

        index_body = (
            "<h1>AI API 中转站竞品监测报告</h1>"
            f"<p class=\"muted\">静态导出时间：{_local(utc_now())}（北京时间）</p>"
            "<section class=\"card\"><table><thead><tr><th>生成时间</th><th>报告</th>"
            "<th>统计窗口</th></tr></thead><tbody>"
            + ("".join(report_rows) or "<tr><td colspan=\"3\">暂无报告</td></tr>")
            + "</tbody></table></section>"
        )
        (output / "index.html").write_text(_layout("竞品监测报告", index_body), encoding="utf-8")

        change_rows = []
        for change, target in changes:
            source = _safe_url(change.source_url)
            source_link = f'<a href="{source}" rel="noreferrer">原文</a>' if source else "—"
            percent = "—" if change.change_percent is None else f"{change.change_percent:.4f}%"
            change_rows.append(
                "<tr>"
                f"<td>{_local(change.detected_at)}</td>"
                f"<td>{escape(GROUP_LABELS.get(target.group_code, target.group_code))} / {escape(target.name)}</td>"
                f"<td>{escape(change.change_type)}</td><td>{escape(change.summary)}</td>"
                f"<td>{escape(change.old_value or '—')} → {escape(change.new_value or '—')}</td>"
                f"<td>{percent}</td><td><span class=\"badge\">{escape(change.verify_status)}</span></td>"
                f"<td>{source_link}</td></tr>"
            )
        changes_body = (
            f"<h1>变动记录</h1><p class=\"muted\">导出最近 {len(changes)} 条，按检测时间倒序。</p>"
            "<table><thead><tr><th>时间（北京）</th><th>类别 / 对象</th><th>类型</th><th>摘要</th>"
            "<th>旧值 → 新值</th><th>幅度</th><th>核对状态</th><th>来源</th></tr></thead><tbody>"
            + ("".join(change_rows) or "<tr><td colspan=\"8\">暂无变动</td></tr>")
            + "</tbody></table>"
        )
        (output / "changes.html").write_text(_layout("变动记录", changes_body), encoding="utf-8")
    (output / ".nojekyll").write_text("", encoding="utf-8")
    return {"reports": len(reports), "changes": len(changes)}


if __name__ == "__main__":
    print(export_site(Path(__file__).resolve().parents[1] / "docs"))
