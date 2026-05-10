"""Pillar 3 — Microstructure Pulse.

Composes five microstructure signals (CVD velocity, liquidation Hawkes
hazard, spread compression, taker aggression, book pressure) into a single
[0,1] score via a weighted linear combination passed through a sigmoid.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List, Optional, Sequence

from ..core.config_loader import MPCfg
from ..core.math_utils import (
    ewma,
    hawkes_intensity,
    numerical_derivative,
    sigmoid,
)
from ..data.binance_client import AggTrade, BookTicker, DepthSnapshot
from ..data.liq_buffer import Liquidation


@dataclass
class MPComponents:
    cvd_velocity: float
    liq_hazard: float            # events / minute
    spread_compression: float    # current_spread / ewma_spread
    taker_aggression: float
    book_pressure: float


@dataclass
class MPResult:
    passed: bool
    score: float
    components: MPComponents
    reason: str


# ---- individual component calculators (pure) ----

def cvd_velocity(trades: Sequence[AggTrade], window_seconds: float = 30.0) -> float:
    """Second derivative of cumulative volume delta across the window.

    CVD is built tick-by-tick over the window; the velocity is computed by
    sampling CVD at start / midpoint / end and applying a 3-point stencil so
    a steady flow rate cleanly produces zero and an accelerating flow
    produces a non-zero value.
    """
    if len(trades) < 3:
        return 0.0
    sorted_t = sorted(trades, key=lambda t: t.timestamp_ms)
    cutoff = sorted_t[-1].timestamp_ms - int(window_seconds * 1000)
    window = [t for t in sorted_t if t.timestamp_ms >= cutoff]
    if len(window) < 3:
        return 0.0
    cvd_series: List[float] = []
    running = 0.0
    for t in window:
        running += t.signed_qty
        cvd_series.append(running)
    n = len(cvd_series)
    sampled = [cvd_series[0], cvd_series[n // 2], cvd_series[-1]]
    dt = max(window_seconds / 2.0, 1e-3)
    return numerical_derivative(sampled, dt)


def liq_hazard(liqs: Sequence[Liquidation],
               base_rate: float = 0.0,
               decay: float = 0.05) -> float:
    """Hawkes intensity scaled to events/minute."""
    if not liqs:
        return base_rate * 60.0
    now_s = time.time()
    times = [lq.timestamp_ms / 1000.0 for lq in liqs]
    intensity_per_sec = hawkes_intensity(times, now_s, base_rate, decay)
    return intensity_per_sec * 60.0


def spread_compression(book: BookTicker, recent_spreads: Sequence[float],
                       half_life_samples: float = 60.0) -> float:
    """current_spread / EWMA(spread). Lower is tighter (compressed)."""
    if book.spread <= 0 or not recent_spreads:
        return 1.0
    ewma_val = ewma(recent_spreads, half_life_samples)
    if ewma_val <= 0:
        return 1.0
    return book.spread / ewma_val


def taker_aggression(trades: Sequence[AggTrade],
                     window_seconds: float = 30.0) -> float:
    """(buy_vol - sell_vol) / total_vol over last window."""
    if not trades:
        return 0.0
    sorted_t = sorted(trades, key=lambda t: t.timestamp_ms)
    cutoff = sorted_t[-1].timestamp_ms - int(window_seconds * 1000)
    buy_vol = sell_vol = 0.0
    for t in sorted_t:
        if t.timestamp_ms < cutoff:
            continue
        if t.is_buyer_maker:
            sell_vol += t.qty
        else:
            buy_vol += t.qty
    total = buy_vol + sell_vol
    if total <= 0:
        return 0.0
    return (buy_vol - sell_vol) / total


def book_pressure(depth: DepthSnapshot, mid: float, band_pct: float = 0.001) -> float:
    """(bid_within_band - ask_within_band) / total_within_band."""
    if mid <= 0:
        return 0.0
    bid_lo = mid * (1.0 - band_pct)
    ask_hi = mid * (1.0 + band_pct)
    bid_sum = sum(qty for px, qty in depth.bids if px >= bid_lo)
    ask_sum = sum(qty for px, qty in depth.asks if px <= ask_hi)
    total = bid_sum + ask_sum
    if total <= 0:
        return 0.0
    return (bid_sum - ask_sum) / total


# ---- composite ----

def compose_score(components: MPComponents, cfg: MPCfg) -> float:
    """Weighted linear combination -> sigmoid in [0,1]."""
    w = cfg.weights
    # spread_compression: 1 == neutral, <1 == tight (good), >1 == wide (bad).
    inv_spread = 1.0 / max(components.spread_compression, 1e-6)
    raw = (
        w.cvd_velocity * _scale(components.cvd_velocity, 1.0)
        + w.liq_hazard * _scale(components.liq_hazard, 5.0)
        + w.spread_compression * _scale(inv_spread - 1.0, 0.5)
        + w.taker_aggression * components.taker_aggression
        + w.book_pressure * components.book_pressure
    )
    return sigmoid(raw - cfg.bias + 0.5)


def _scale(value: float, ref: float) -> float:
    """Soft-clamp to roughly [-1, 1]."""
    if ref <= 0:
        return 0.0
    return max(-1.0, min(1.0, value / ref))


def evaluate_mp(
    trades: Sequence[AggTrade],
    liqs: Sequence[Liquidation],
    book: BookTicker,
    depth: DepthSnapshot,
    recent_spreads: Sequence[float],
    cfg: MPCfg,
    liq_coverage_seconds: Optional[float] = None,
) -> MPResult:
    """Compute MP gate. If liquidation buffer coverage < 10s, fail with reason."""
    if liq_coverage_seconds is not None and liq_coverage_seconds < 10.0:
        components = MPComponents(0.0, 0.0, 1.0, 0.0, 0.0)
        return MPResult(False, 0.0, components,
                        f"insufficient liq coverage ({liq_coverage_seconds:.1f}s)")

    comps = MPComponents(
        cvd_velocity=cvd_velocity(trades),
        liq_hazard=liq_hazard(liqs),
        spread_compression=spread_compression(book, recent_spreads),
        taker_aggression=taker_aggression(trades),
        book_pressure=book_pressure(depth, book.mid),
    )
    score = compose_score(comps, cfg)
    reasons = []
    passed = True
    if score < cfg.gate:
        passed = False
        reasons.append(f"score {score:.2f} < gate {cfg.gate:.2f}")
    if comps.liq_hazard < cfg.hazard_min_rate:
        passed = False
        reasons.append(
            f"hazard {comps.liq_hazard:.2f}/min < {cfg.hazard_min_rate:.2f}",
        )
    return MPResult(
        passed=passed,
        score=score,
        components=comps,
        reason="; ".join(reasons) if reasons else "ok",
    )
