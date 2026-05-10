"""Trade plan math tests with synthetic gate output."""

from __future__ import annotations

from src.core.config_loader import (
    EntryCfg,
    FBPDCfg,
    GlobalCfg,
    LRMCfg,
    MPCfg,
    MPWeights,
    PairCfg,
)
from src.decision.direction import DirectionVote
from src.decision.trade_plan import build_trade_plan, confidence_score
from src.pillars.fbpd import FBPDResult
from src.pillars.lrm import LRMResult
from src.pillars.mp import MPComponents, MPResult
from src.pillars.mts import MTSResult


def _pair_cfg() -> PairCfg:
    return PairCfg(
        maintenance_margin=0.005,
        max_leverage=5,
        quarterly_symbol="BTCUSDT_260626",
        lrm=LRMCfg(proximity_band_atr=0.5, min_cluster_density=0.5,
                   min_oi_usd=1.0, kernel_bandwidth_pct=0.002),
        fbpd=FBPDCfg(min_divergence=0.001, funding_filter=0.0001),
        mp=MPCfg(
            gate=0.65, hazard_min_rate=0.0,
            weights=MPWeights(cvd_velocity=0.25, liq_hazard=0.30,
                              spread_compression=0.15, taker_aggression=0.20,
                              book_pressure=0.10),
            bias=0.5,
        ),
        entry=EntryCfg(limit_offset_pct=0.05),
    )


def _global_cfg() -> GlobalCfg:
    return GlobalCfg(default_equity_usd=1000.0, risk_per_trade_pct=2.0,
                     liq_buffer_seconds=60, log_path="logs/x.jsonl")


def _gates(direction_long: bool = True):
    cluster = 67100.0 if direction_long else 67700.0  # below price for LONG
    lrm = LRMResult(passed=True, score=0.8, cluster_price=cluster,
                    oi_usd=200_000_000, distance_atr=0.3,
                    side="long_liqs" if direction_long else "short_liqs",
                    reason="ok")
    fbpd = FBPDResult(passed=True, score=-0.0018, perp_price=67400.0,
                      fair_price=67450.0, funding_rate=0.0002,
                      days_to_expiry=60.0, available=True, reason="ok")
    mp = MPResult(
        passed=True, score=0.7,
        components=MPComponents(0.4, 3.0, 0.9, 0.3, 0.2),
        reason="ok",
    )
    mts = MTSResult(state="NORMAL", passed=True, mp_gate_multiplier=1.0,
                    next_event=None, reason="ok")
    return lrm, fbpd, mp, mts


def test_long_setup_produces_valid_plan():
    lrm, fbpd, mp, mts = _gates(direction_long=True)
    direction = DirectionVote("LONG", 0.6, 1.0, 0.4)
    plan = build_trade_plan(
        pair="BTCUSDT", market_price=67400.0, atr_value=120.0,
        lrm=lrm, fbpd=fbpd, mp=mp, mts=mts,
        direction_vote=direction, pair_cfg=_pair_cfg(), global_cfg=_global_cfg(),
        equity_usd=1000.0,
    )
    assert plan.valid, plan.reason
    assert plan.direction == "LONG"
    assert plan.entry_price > 0
    assert plan.stop_loss < plan.entry_price
    assert plan.take_profit_1 > plan.entry_price
    assert plan.take_profit_2 > plan.take_profit_1
    assert plan.leverage_capped <= 5
    # 2.5R for TP2 should be 2.5 * SL_distance from entry.
    sl_d = plan.entry_price - plan.stop_loss
    assert abs((plan.take_profit_2 - plan.entry_price) - 2.5 * sl_d) < 5.0


def test_short_setup_mirrors_long():
    lrm, fbpd, mp, mts = _gates(direction_long=False)
    direction = DirectionVote("SHORT", 0.6, 0.3, 1.0)
    plan = build_trade_plan(
        pair="BTCUSDT", market_price=67400.0, atr_value=120.0,
        lrm=lrm, fbpd=fbpd, mp=mp, mts=mts,
        direction_vote=direction, pair_cfg=_pair_cfg(), global_cfg=_global_cfg(),
        equity_usd=1000.0,
    )
    assert plan.valid, plan.reason
    assert plan.direction == "SHORT"
    assert plan.stop_loss > plan.entry_price
    assert plan.take_profit_1 < plan.entry_price
    assert plan.take_profit_2 < plan.take_profit_1


def test_sl_too_wide_rejects_setup():
    lrm, fbpd, mp, mts = _gates(direction_long=True)
    # Cluster very far below current price => SL too wide.
    lrm.cluster_price = 60000.0
    direction = DirectionVote("LONG", 0.6, 1.0, 0.4)
    plan = build_trade_plan(
        pair="BTCUSDT", market_price=67400.0, atr_value=120.0,
        lrm=lrm, fbpd=fbpd, mp=mp, mts=mts,
        direction_vote=direction, pair_cfg=_pair_cfg(), global_cfg=_global_cfg(),
        equity_usd=1000.0,
    )
    assert not plan.valid
    assert "SL distance" in plan.reason


def test_leverage_caps_at_pair_max():
    lrm, fbpd, mp, mts = _gates(direction_long=True)
    direction = DirectionVote("LONG", 0.6, 1.0, 0.4)
    plan = build_trade_plan(
        pair="BTCUSDT", market_price=67400.0, atr_value=120.0,
        lrm=lrm, fbpd=fbpd, mp=mp, mts=mts,
        direction_vote=direction, pair_cfg=_pair_cfg(), global_cfg=_global_cfg(),
        equity_usd=100.0,   # tiny equity => big leverage demanded
    )
    assert plan.leverage_capped == 5
    assert plan.leverage_recommended >= 5


def test_confidence_score_normal_state():
    lrm, fbpd, mp, mts = _gates(direction_long=True)
    c = confidence_score(lrm, fbpd, mp, mts)
    assert 0.0 <= c <= 1.0
