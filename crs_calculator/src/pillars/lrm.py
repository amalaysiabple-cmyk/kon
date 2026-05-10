"""Pillar 1 — Leverage Resonance Map.

Estimates liquidation-cluster price levels from open interest, builds a
Gaussian density over those levels, and reports the nearest cluster as a
potential price magnet.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple

import numpy as np

from ..core.config_loader import LRMCfg
from ..core.math_utils import gaussian_kernel

# Common retail leverage tiers and rough OI weights.
LEVERAGE_TIERS: Tuple[Tuple[int, float], ...] = (
    (5, 0.10),
    (10, 0.20),
    (25, 0.30),
    (50, 0.25),
    (100, 0.15),
)


@dataclass
class LRMResult:
    passed: bool
    score: float                 # density at nearest cluster (0..1)
    cluster_price: Optional[float]
    oi_usd: float
    distance_atr: float
    side: str                    # "long_liqs" or "short_liqs" or "none"
    reason: str


def liquidation_price(entry: float, leverage: int, maintenance_margin: float,
                      side: str) -> float:
    """Approximate isolated-margin liquidation price.

    side='long'  => long position liquidation price (below entry)
    side='short' => short position liquidation price (above entry)
    """
    if leverage <= 0:
        raise ValueError("leverage must be > 0")
    if side == "long":
        return entry * (1.0 - 1.0 / leverage + maintenance_margin)
    if side == "short":
        return entry * (1.0 + 1.0 / leverage - maintenance_margin)
    raise ValueError("side must be 'long' or 'short'")


def build_cluster_levels(
    current_price: float,
    long_short_ratio: float,
    maintenance_margin: float,
    oi_usd_total: float,
) -> List[Tuple[float, float, str]]:
    """Return list of (price, oi_usd_at_level, side)."""
    long_share = long_short_ratio / (1.0 + long_short_ratio)
    short_share = 1.0 - long_share
    out: List[Tuple[float, float, str]] = []
    for lev, weight in LEVERAGE_TIERS:
        long_px = liquidation_price(current_price, lev, maintenance_margin, "long")
        short_px = liquidation_price(current_price, lev, maintenance_margin, "short")
        out.append((long_px, oi_usd_total * weight * long_share, "long_liqs"))
        out.append((short_px, oi_usd_total * weight * short_share, "short_liqs"))
    return out


def density_grid(
    levels: Iterable[Tuple[float, float, str]],
    current_price: float,
    bandwidth_pct: float,
    span_pct: float = 0.10,
    n_points: int = 401,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Build a 1-D density grid weighted by per-level OI USD.

    Returns (prices, density, oi_at_grid).
    `density` is normalized to [0,1] by its max value (defensive guard for
    empty/zero arrays included).
    """
    lo = current_price * (1.0 - span_pct)
    hi = current_price * (1.0 + span_pct)
    grid = np.linspace(lo, hi, n_points)
    bw = current_price * bandwidth_pct
    density = np.zeros_like(grid)
    oi_at_grid = np.zeros_like(grid)
    total_weight = sum(max(w, 0.0) for _, w, _ in levels) or 1.0
    for px, w, _side in levels:
        if w <= 0:
            continue
        kern = gaussian_kernel(grid, px, bw)
        density += (w / total_weight) * kern
        oi_at_grid += w * kern
    if density.max() > 0:
        density = density / density.max()
    return grid, density, oi_at_grid


def evaluate_lrm(
    current_price: float,
    open_interest_coin: float,
    long_short_ratio: float,
    maintenance_margin: float,
    atr_value: float,
    cfg: LRMCfg,
) -> LRMResult:
    """Run LRM gate. `open_interest_coin` is in base units (BTC, ETH, ...).

    The OI-USD figure is approximated as `open_interest_coin * current_price`,
    which matches what Binance shows on the OI dashboard.
    """
    if current_price <= 0 or atr_value <= 0:
        return LRMResult(False, 0.0, None, 0.0, 0.0, "none",
                         "invalid market inputs (price/ATR <= 0)")

    oi_usd_total = open_interest_coin * current_price
    levels = build_cluster_levels(
        current_price, long_short_ratio, maintenance_margin, oi_usd_total,
    )
    grid, density, oi_at_grid = density_grid(
        levels, current_price, cfg.kernel_bandwidth_pct,
    )

    # Nearest peak (local maximum) by price proximity.
    peak_idx = _nearest_peak_to(grid, density, current_price)
    if peak_idx is None:
        return LRMResult(False, 0.0, None, 0.0, 0.0, "none",
                         "no density peak detected")

    cluster_price = float(grid[peak_idx])
    cluster_density = float(density[peak_idx])
    cluster_oi = float(oi_at_grid[peak_idx])
    distance_atr = abs(current_price - cluster_price) / atr_value

    # Determine which side dominates at that price by re-summing per side.
    long_w = sum(w for px, w, side in levels
                 if side == "long_liqs"
                 and abs(px - cluster_price) <= cfg.kernel_bandwidth_pct * current_price * 2)
    short_w = sum(w for px, w, side in levels
                  if side == "short_liqs"
                  and abs(px - cluster_price) <= cfg.kernel_bandwidth_pct * current_price * 2)
    side = "long_liqs" if long_w >= short_w else "short_liqs"

    reasons = []
    passed = True
    if cluster_density < cfg.min_cluster_density:
        passed = False
        reasons.append(f"density {cluster_density:.2f} < {cfg.min_cluster_density:.2f}")
    if cluster_oi < cfg.min_oi_usd:
        passed = False
        reasons.append(f"oi {cluster_oi:,.0f} < {cfg.min_oi_usd:,.0f}")
    if distance_atr > cfg.proximity_band_atr:
        passed = False
        reasons.append(f"distance {distance_atr:.2f}ATR > {cfg.proximity_band_atr:.2f}ATR")

    return LRMResult(
        passed=passed,
        score=cluster_density,
        cluster_price=cluster_price,
        oi_usd=cluster_oi,
        distance_atr=distance_atr,
        side=side,
        reason="; ".join(reasons) if reasons else "ok",
    )


def _nearest_peak_to(grid: np.ndarray, density: np.ndarray,
                     ref_price: float) -> Optional[int]:
    """Return index of the local-max bin nearest `ref_price`."""
    if density.size == 0 or density.max() <= 0:
        return None
    peak_mask = np.zeros_like(density, dtype=bool)
    peak_mask[1:-1] = (density[1:-1] >= density[:-2]) & (density[1:-1] >= density[2:])
    peak_mask[0] = density[0] >= density[1] if density.size > 1 else True
    peak_mask[-1] = density[-1] >= density[-2] if density.size > 1 else True
    peak_indices = np.where(peak_mask & (density > 0.05))[0]
    if peak_indices.size == 0:
        return int(np.argmax(density))
    distances = np.abs(grid[peak_indices] - ref_price)
    return int(peak_indices[np.argmin(distances)])
