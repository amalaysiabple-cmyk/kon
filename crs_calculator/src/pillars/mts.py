"""Pillar 4 — Macro-Tick Synchronizer.

A small state machine over the macro event calendar plus funding settlements.
Returns a state plus an optional MP-gate multiplier the caller applies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from ..core.config_loader import EventEntry, MTSCfg


@dataclass
class MTSResult:
    state: str                    # NORMAL | BLOCKED | BOOSTED | COOLDOWN
    passed: bool
    mp_gate_multiplier: float
    next_event: Optional[EventEntry]
    reason: str


def _next_funding_time(now: datetime, hours: List[int]) -> datetime:
    """Earliest UTC funding settlement time strictly after `now`."""
    candidates: List[datetime] = []
    for offset in (0, 1):
        day = (now + timedelta(days=offset)).date()
        for h in hours:
            t = datetime(day.year, day.month, day.day, h, 0, 0, tzinfo=timezone.utc)
            if t > now:
                candidates.append(t)
    return min(candidates)


def evaluate_mts(
    events: List[EventEntry],
    cfg: MTSCfg,
    now: Optional[datetime] = None,
) -> MTSResult:
    now_dt = now or datetime.now(timezone.utc)
    high_impact = [e for e in events if e.impact == "high"]

    upcoming = sorted(
        [e for e in high_impact if e.datetime_utc >= now_dt - timedelta(hours=24)],
        key=lambda e: e.datetime_utc,
    )
    next_event = upcoming[0] if upcoming else None

    # 1. Hard block window around scheduled events.
    for e in high_impact:
        delta = (e.datetime_utc - now_dt).total_seconds() / 60.0
        if -cfg.block_after_minutes <= delta <= cfg.block_before_minutes:
            return MTSResult(
                state="BLOCKED",
                passed=False,
                mp_gate_multiplier=1.0,
                next_event=e,
                reason=f"within ±block window of {e.name} "
                       f"({delta:+.1f}m)",
            )
        # 2. Boost window after the event (caller relaxes MP gate).
        lo, hi = cfg.boost_window_minutes
        elapsed = (now_dt - e.datetime_utc).total_seconds() / 60.0
        if lo <= elapsed <= hi:
            return MTSResult(
                state="BOOSTED",
                passed=True,
                mp_gate_multiplier=1.0 / cfg.boost_multiplier,
                next_event=next_event,
                reason=f"post-{e.name} boost ({elapsed:.1f}m after)",
            )

    # 3. Cooldown around funding settlements.
    funding_dt = _next_funding_time(now_dt, cfg.funding_settlement_hours_utc)
    minutes_to_funding = (funding_dt - now_dt).total_seconds() / 60.0
    if minutes_to_funding <= cfg.funding_proximity_minutes:
        return MTSResult(
            state="COOLDOWN",
            passed=True,
            mp_gate_multiplier=1.05,  # slightly stricter MP gate
            next_event=next_event,
            reason=f"{minutes_to_funding:.1f}m to funding",
        )

    return MTSResult(
        state="NORMAL",
        passed=True,
        mp_gate_multiplier=1.0,
        next_event=next_event,
        reason="ok",
    )
