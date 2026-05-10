"""Compute entry / SL / TP / sizing / leverage / order type from gate output."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..core.config_loader import GlobalCfg, PairCfg
from ..pillars.fbpd import FBPDResult
from ..pillars.lrm import LRMResult
from ..pillars.mp import MPResult
from ..pillars.mts import MTSResult
from .direction import DirectionVote


@dataclass
class TradePlan:
    valid: bool
    reason: str
    direction: str
    confidence: float
    conviction: float

    entry_type: str            # MARKET | LIMIT | LIMIT_POST_ONLY
    entry_price: float
    market_price: float

    stop_loss: float
    sl_distance_pct: float
    take_profit_1: float
    take_profit_2: float
    rr_tp1: float
    rr_tp2: float

    leverage_recommended: int
    leverage_capped: int
    size_usd: float
    size_coin: float
    risk_usd_target: float
    risk_usd_actual: float
    equity_usd: float
    notes: str


def confidence_score(lrm: LRMResult, fbpd: FBPDResult, mp: MPResult,
                     mts: MTSResult) -> float:
    fbpd_term = min(abs(fbpd.score) * 200.0, 1.0) if fbpd.available else 0.0
    if mts.state == "NORMAL":
        mts_term = 0.5
    elif mts.state == "BOOSTED":
        mts_term = 0.7
    elif mts.state == "COOLDOWN":
        mts_term = 0.4
    else:
        mts_term = 0.0
    return (lrm.score + fbpd_term + mp.score + mts_term) / 4.0


def _round_coin(value: float, pair: str) -> float:
    if pair.startswith("BTC"):
        return round(value, 4)
    if pair.startswith("ETH"):
        return round(value, 3)
    return round(value, 2)


def _round_price(value: float, pair: str) -> float:
    if pair.startswith("BTC") or pair.startswith("ETH"):
        return round(value, 2)
    return round(value, 4)


def build_trade_plan(
    pair: str,
    market_price: float,
    atr_value: float,
    lrm: LRMResult,
    fbpd: FBPDResult,
    mp: MPResult,
    mts: MTSResult,
    direction_vote: DirectionVote,
    pair_cfg: PairCfg,
    global_cfg: GlobalCfg,
    equity_usd: Optional[float] = None,
) -> TradePlan:
    equity = equity_usd if equity_usd is not None else global_cfg.default_equity_usd
    risk_usd_target = equity * (global_cfg.risk_per_trade_pct / 100.0)
    direction = direction_vote.direction
    confidence = confidence_score(lrm, fbpd, mp, mts)
    conviction = direction_vote.conviction

    # Entry price (favorable side limit, vs market)
    offset = pair_cfg.entry.limit_offset_pct / 100.0
    if direction == "LONG":
        limit_price = market_price * (1.0 - offset)
    else:
        limit_price = market_price * (1.0 + offset)

    # Order type selection.
    spread_comp = mp.components.spread_compression
    if mp.score > 0.75 and spread_comp <= 1.2:
        entry_type = "MARKET"
        entry_price = market_price
    elif fbpd.available and abs(fbpd.score) >= pair_cfg.fbpd.min_divergence * 3.0:
        entry_type = "LIMIT_POST_ONLY"
        entry_price = limit_price
    else:
        entry_type = "LIMIT"
        entry_price = limit_price

    # Stop loss derivation from LRM cluster + ATR buffer.
    cluster = lrm.cluster_price
    buffer = 0.1 * atr_value
    if direction == "LONG":
        if cluster is not None and cluster < entry_price:
            sl = cluster - buffer
        else:
            sl = entry_price - max(atr_value, entry_price * 0.003)
        if sl >= entry_price:
            sl = entry_price * (1.0 - 0.003)
    else:
        if cluster is not None and cluster > entry_price:
            sl = cluster + buffer
        else:
            sl = entry_price + max(atr_value, entry_price * 0.003)
        if sl <= entry_price:
            sl = entry_price * (1.0 + 0.003)

    sl_distance = abs(entry_price - sl)
    sl_distance_pct = sl_distance / entry_price * 100.0

    # SL distance sanity (0.3% .. 1.5%).
    if sl_distance_pct < 0.3 or sl_distance_pct > 1.5:
        return TradePlan(
            valid=False,
            reason=f"SL distance {sl_distance_pct:.2f}% outside [0.30%, 1.50%]",
            direction=direction, confidence=confidence, conviction=conviction,
            entry_type=entry_type, entry_price=_round_price(entry_price, pair),
            market_price=_round_price(market_price, pair),
            stop_loss=_round_price(sl, pair), sl_distance_pct=sl_distance_pct,
            take_profit_1=0.0, take_profit_2=0.0, rr_tp1=0.0, rr_tp2=0.0,
            leverage_recommended=0, leverage_capped=0,
            size_usd=0.0, size_coin=0.0,
            risk_usd_target=risk_usd_target, risk_usd_actual=0.0,
            equity_usd=equity, notes="",
        )

    # Take profits.
    if direction == "LONG":
        tp1 = entry_price + 1.0 * sl_distance
        tp2 = entry_price + 2.5 * sl_distance
    else:
        tp1 = entry_price - 1.0 * sl_distance
        tp2 = entry_price - 2.5 * sl_distance

    # Sizing.
    size_usd_uncapped = risk_usd_target / (sl_distance_pct / 100.0)
    leverage_recommended = max(1, int(round(size_usd_uncapped / max(equity, 1e-9))))
    leverage_capped = min(leverage_recommended, pair_cfg.max_leverage)
    size_usd = min(size_usd_uncapped, equity * leverage_capped)
    size_coin = size_usd / entry_price if entry_price > 0 else 0.0
    risk_usd_actual = size_usd * (sl_distance_pct / 100.0)

    notes = []
    if entry_type.startswith("LIMIT"):
        notes.append("Wait for limit fill, max 90s; otherwise re-calculate.")
    if leverage_recommended > leverage_capped:
        notes.append(
            f"Leverage capped from {leverage_recommended}x to {leverage_capped}x "
            f"(pair max).",
        )

    return TradePlan(
        valid=True,
        reason="ok",
        direction=direction,
        confidence=confidence,
        conviction=conviction,
        entry_type=entry_type,
        entry_price=_round_price(entry_price, pair),
        market_price=_round_price(market_price, pair),
        stop_loss=_round_price(sl, pair),
        sl_distance_pct=sl_distance_pct,
        take_profit_1=_round_price(tp1, pair),
        take_profit_2=_round_price(tp2, pair),
        rr_tp1=1.0,
        rr_tp2=2.5,
        leverage_recommended=leverage_recommended,
        leverage_capped=leverage_capped,
        size_usd=round(size_usd, 2),
        size_coin=_round_coin(size_coin, pair),
        risk_usd_target=round(risk_usd_target, 2),
        risk_usd_actual=round(risk_usd_actual, 2),
        equity_usd=equity,
        notes=" ".join(notes),
    )
