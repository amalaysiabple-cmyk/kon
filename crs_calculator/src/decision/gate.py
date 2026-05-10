"""Quadruple-gate evaluator: orchestrates data fetching, runs all four
pillars, and produces a setup decision plus optional trade plan.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Deque, Dict, List, Optional

from ..core.config_loader import Config, EventEntry
from ..data.binance_client import BinanceFuturesClient, atr
from ..data.liq_buffer import LiquidationBuffer
from ..pillars.fbpd import FBPDResult, evaluate_fbpd
from ..pillars.lrm import LRMResult, evaluate_lrm
from ..pillars.mp import MPResult, evaluate_mp
from ..pillars.mts import MTSResult, evaluate_mts
from .direction import DirectionVote, infer_direction
from .trade_plan import TradePlan, build_trade_plan


@dataclass
class GateOutcome:
    pair: str
    timestamp: datetime
    valid: bool
    reasons: List[str] = field(default_factory=list)
    lrm: Optional[LRMResult] = None
    fbpd: Optional[FBPDResult] = None
    mp: Optional[MPResult] = None
    mts: Optional[MTSResult] = None
    direction: Optional[DirectionVote] = None
    plan: Optional[TradePlan] = None
    market_price: float = 0.0
    raw_snapshot: Dict = field(default_factory=dict)


class GateEvaluator:
    """Stateful evaluator that owns long-lived caches (recent spreads etc.)."""

    def __init__(self, config: Config, events: List[EventEntry],
                 liq_buffer: LiquidationBuffer):
        self.config = config
        self.events = events
        self.liq_buffer = liq_buffer
        self._spread_history: Dict[str, Deque[float]] = {}

    def _record_spread(self, pair: str, spread: float) -> None:
        buf = self._spread_history.setdefault(pair, deque(maxlen=240))
        buf.append(spread)

    def _spreads(self, pair: str) -> List[float]:
        return list(self._spread_history.get(pair, []))

    async def evaluate(
        self,
        pair: str,
        client: BinanceFuturesClient,
        equity_usd: Optional[float] = None,
    ) -> GateOutcome:
        cfg = self.config
        if pair not in cfg.pairs:
            return GateOutcome(
                pair=pair,
                timestamp=datetime.now(timezone.utc),
                valid=False,
                reasons=[f"pair {pair} not configured"],
            )
        pair_cfg = cfg.pairs[pair]

        # Fetch in parallel where possible.
        oi_t = client.open_interest(pair)
        ratio_t = client.top_long_short_position_ratio(pair, period="5m", limit=1)
        prem_t = client.premium_index(pair)
        book_t = client.book_ticker(pair)
        trades_t = client.agg_trades(pair, limit=200)
        depth_t = client.depth(pair, limit=20)
        kl_t = client.klines(pair, interval="15m", limit=50)

        oi, ratio_list, premium, book, trades, depth, klines = await asyncio.gather(
            oi_t, ratio_t, prem_t, book_t, trades_t, depth_t, kl_t,
        )

        # Quarterly price is best-effort; missing => FBPD becomes N/A.
        quarterly_price: Optional[float] = None
        try:
            quarterly_price = await client.ticker_price(pair_cfg.quarterly_symbol)
        except Exception:  # noqa: BLE001
            quarterly_price = None

        long_short_ratio = (
            ratio_list[0].long_short_ratio if ratio_list else 1.0
        )
        atr_15 = atr(klines, period=14)
        market_price = book.mid if book.mid > 0 else premium.mark_price
        self._record_spread(pair, book.spread)

        # MTS first — short-circuit if BLOCKED.
        mts_res = evaluate_mts(self.events, cfg.mts)

        # LRM
        lrm_res = evaluate_lrm(
            current_price=market_price,
            open_interest_coin=oi,
            long_short_ratio=long_short_ratio,
            maintenance_margin=pair_cfg.maintenance_margin,
            atr_value=atr_15,
            cfg=pair_cfg.lrm,
        )

        # FBPD
        fbpd_res = evaluate_fbpd(
            perp_price=premium.mark_price,
            quarterly_price=quarterly_price,
            funding_rate=premium.last_funding_rate,
            quarterly_symbol=pair_cfg.quarterly_symbol,
            cfg=pair_cfg.fbpd,
        )

        # MP — apply MTS gate multiplier
        mp_cfg = pair_cfg.mp.model_copy(update={
            "gate": pair_cfg.mp.gate * mts_res.mp_gate_multiplier
        })
        liqs = self.liq_buffer.snapshot(pair)
        mp_res = evaluate_mp(
            trades=trades,
            liqs=liqs,
            book=book,
            depth=depth,
            recent_spreads=self._spreads(pair),
            cfg=mp_cfg,
            liq_coverage_seconds=self.liq_buffer.coverage_seconds(),
        )

        # Setup is valid only if all pass; FBPD N/A is treated as failed pass-through.
        gates_pass = [
            lrm_res.passed,
            fbpd_res.passed and fbpd_res.available,
            mp_res.passed,
            mts_res.passed,
        ]
        valid = all(gates_pass)

        reasons: List[str] = []
        if not lrm_res.passed:
            reasons.append(f"LRM: {lrm_res.reason}")
        if not fbpd_res.passed:
            reasons.append(f"FBPD: {fbpd_res.reason}")
        if not mp_res.passed:
            reasons.append(f"MP: {mp_res.reason}")
        if not mts_res.passed:
            reasons.append(f"MTS: {mts_res.reason}")

        direction = infer_direction(market_price, lrm_res, fbpd_res, mp_res)

        plan: Optional[TradePlan] = None
        if valid:
            plan = build_trade_plan(
                pair=pair,
                market_price=market_price,
                atr_value=atr_15,
                lrm=lrm_res,
                fbpd=fbpd_res,
                mp=mp_res,
                mts=mts_res,
                direction_vote=direction,
                pair_cfg=pair_cfg,
                global_cfg=cfg.global_,
                equity_usd=equity_usd,
            )
            if not plan.valid:
                valid = False
                reasons.append(f"PLAN: {plan.reason}")

        snapshot = {
            "open_interest_coin": oi,
            "long_short_ratio": long_short_ratio,
            "mark_price": premium.mark_price,
            "index_price": premium.index_price,
            "funding_rate": premium.last_funding_rate,
            "quarterly_price": quarterly_price,
            "atr_15m": atr_15,
            "spread": book.spread,
            "bid": book.bid_price,
            "ask": book.ask_price,
            "n_trades": len(trades),
            "n_liqs": len(liqs),
            "liq_coverage_s": self.liq_buffer.coverage_seconds(),
        }

        return GateOutcome(
            pair=pair,
            timestamp=datetime.now(timezone.utc),
            valid=valid,
            reasons=reasons,
            lrm=lrm_res,
            fbpd=fbpd_res,
            mp=mp_res,
            mts=mts_res,
            direction=direction,
            plan=plan,
            market_price=market_price,
            raw_snapshot=snapshot,
        )
