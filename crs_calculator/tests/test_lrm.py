"""LRM unit tests with synthetic OI data."""

from __future__ import annotations

from src.core.config_loader import LRMCfg
from src.pillars.lrm import (
    build_cluster_levels,
    density_grid,
    evaluate_lrm,
    liquidation_price,
)


def _cfg(**overrides) -> LRMCfg:
    base = dict(
        proximity_band_atr=1.0,
        min_cluster_density=0.5,
        min_oi_usd=1.0,
        kernel_bandwidth_pct=0.002,
    )
    base.update(overrides)
    return LRMCfg(**base)


def test_liquidation_price_long_below_entry():
    px = liquidation_price(100.0, 10, 0.005, "long")
    assert px < 100.0
    # 10x => roughly 10% drop, plus maintenance buffer of 0.5% => ~90.5
    assert abs(px - 90.5) < 0.6


def test_liquidation_price_short_above_entry():
    px = liquidation_price(100.0, 10, 0.005, "short")
    assert px > 100.0
    assert abs(px - 109.5) < 0.6


def test_build_cluster_levels_includes_both_sides():
    levels = build_cluster_levels(
        current_price=100.0, long_short_ratio=1.0,
        maintenance_margin=0.005, oi_usd_total=1_000_000,
    )
    sides = {s for _, _, s in levels}
    assert sides == {"long_liqs", "short_liqs"}
    total_w = sum(w for _, w, _ in levels)
    assert abs(total_w - 1_000_000) < 1.0


def test_evaluate_lrm_pass():
    # Big OI, tight cluster band, ample ATR => should pass.
    res = evaluate_lrm(
        current_price=100.0,
        open_interest_coin=1_000_000,   # $100M OI total
        long_short_ratio=1.0,
        maintenance_margin=0.005,
        atr_value=2.0,                   # 2% ATR
        cfg=_cfg(min_oi_usd=1_000_000, min_cluster_density=0.3,
                 proximity_band_atr=5.0),
    )
    assert res.passed, res.reason
    assert res.cluster_price is not None
    assert res.score > 0


def test_evaluate_lrm_fail_distance():
    res = evaluate_lrm(
        current_price=100.0,
        open_interest_coin=1_000_000,
        long_short_ratio=1.0,
        maintenance_margin=0.005,
        atr_value=0.01,                  # tiny ATR => everything is far
        cfg=_cfg(proximity_band_atr=0.1),
    )
    assert not res.passed
    assert "distance" in res.reason


def test_evaluate_lrm_fail_invalid_inputs():
    res = evaluate_lrm(
        current_price=0.0, open_interest_coin=1.0, long_short_ratio=1.0,
        maintenance_margin=0.005, atr_value=1.0, cfg=_cfg(),
    )
    assert not res.passed


def test_density_grid_normalized():
    levels = build_cluster_levels(100.0, 1.0, 0.005, 1_000_000)
    _, density, _ = density_grid(levels, 100.0, 0.002)
    assert density.max() <= 1.0 + 1e-9
    assert density.max() > 0
