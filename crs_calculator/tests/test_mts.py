"""MTS state machine tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.core.config_loader import EventEntry, MTSCfg
from src.pillars.mts import evaluate_mts


def _cfg() -> MTSCfg:
    return MTSCfg(
        block_before_minutes=15,
        block_after_minutes=5,
        boost_window_minutes=(2, 10),
        boost_multiplier=1.3,
        funding_proximity_minutes=10,
        funding_settlement_hours_utc=[0, 8, 16],
    )


def _ev(name: str, dt: datetime, impact: str = "high") -> EventEntry:
    return EventEntry(name=name, impact=impact, datetime_utc=dt)


def test_normal_state_far_from_events():
    now = datetime(2026, 5, 10, 4, 30, tzinfo=timezone.utc)
    res = evaluate_mts(events=[_ev("CPI", datetime(2026, 5, 13, 12, 30,
                                                    tzinfo=timezone.utc))],
                       cfg=_cfg(), now=now)
    assert res.state == "NORMAL"
    assert res.passed


def test_blocked_within_window():
    target = datetime(2026, 5, 10, 12, 30, tzinfo=timezone.utc)
    now = target - timedelta(minutes=5)
    res = evaluate_mts([_ev("CPI", target)], _cfg(), now=now)
    assert res.state == "BLOCKED"
    assert not res.passed


def test_boosted_after_event():
    target = datetime(2026, 5, 10, 12, 30, tzinfo=timezone.utc)
    now = target + timedelta(minutes=6)
    res = evaluate_mts([_ev("CPI", target)], _cfg(), now=now)
    assert res.state == "BOOSTED"
    assert res.passed
    assert res.mp_gate_multiplier < 1.0


def test_funding_cooldown():
    # 5 min before 16:00 funding settlement
    now = datetime(2026, 5, 10, 15, 55, tzinfo=timezone.utc)
    res = evaluate_mts([], _cfg(), now=now)
    assert res.state == "COOLDOWN"
    assert res.passed
