from __future__ import annotations

from datetime import datetime, timezone

from app.models import Change, Report, Target
from scripts.export_pages import export_site


def test_export_site_writes_read_only_reports_and_changes(session_factory, session, tmp_path):
    target = Target(name="OpenRouter", group_code="A")
    session.add(target)
    session.flush()
    session.add(Change(
        target_id=target.id,
        change_type="新功能",
        summary="静态导出测试",
        signal_type="confirmed",
        severity="medium",
        verify_status="confirmed",
        source_url="https://openrouter.ai/blog/test",
        detected_at=datetime(2026, 11, 12, tzinfo=timezone.utc),
    ))
    report = Report(
        title="静态月报",
        report_kind="monthly",
        period_start=datetime(2026, 10, 13, tzinfo=timezone.utc),
        period_end=datetime(2026, 11, 12, tzinfo=timezone.utc),
        content="# 静态月报\n\n| A | B |\n|---|---|\n| 1 | 2 |\n",
    )
    session.add(report)
    session.commit()

    result = export_site(tmp_path / "site", session_factory)
    assert result == {"reports": 1, "changes": 1}
    index = (tmp_path / "site" / "index.html").read_text(encoding="utf-8")
    detail = (tmp_path / "site" / "reports" / f"{report.id}.html").read_text(encoding="utf-8")
    changes = (tmp_path / "site" / "changes.html").read_text(encoding="utf-8")
    assert 'href="reports/' in index and "静态月报" in index
    assert "<table>" in detail and "GitHub Pages 静态只读镜像" in detail
    assert "正规军竞品 / OpenRouter" in changes
    assert 'href="https://openrouter.ai/blog/test"' in changes
    assert (tmp_path / "site" / ".nojekyll").exists()
