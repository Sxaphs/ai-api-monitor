from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import Change, Target
from app.services.changes import add_change

UTC = timezone.utc


def test_warning_closes_within_14_days_but_not_after_expiry(session):
    target = Target(name="closure", group_code="A")
    session.add(target)
    session.commit()
    base = datetime(2026, 11, 12, tzinfo=UTC)
    warning = add_change(
        session, target=target, snapshot=None, change_type="价格变动", summary="预告",
        signal_type="warning", detected_at=base - timedelta(days=13),
    )
    confirmed = add_change(
        session, target=target, snapshot=None, change_type="降价", summary="已降价",
        old_value="2", new_value="1", change_percent=-50, detected_at=base,
    )
    assert warning.related_id == confirmed.id
    assert confirmed.related_id == warning.id
    assert warning.verify_status == "confirmed"
    assert warning.verified_at is not None

    expired = add_change(
        session, target=target, snapshot=None, change_type="价格变动", summary="过期预告",
        signal_type="warning", detected_at=base - timedelta(days=15),
    )
    unrelated = add_change(
        session, target=target, snapshot=None, change_type="涨价", summary="另一价格",
        detected_at=base + timedelta(minutes=1),
    )
    assert expired.related_id is None
    assert expired.verify_status == "pending"
    assert unrelated.related_id is None
