"""MP component tests with synthetic trade/depth data."""

from __future__ import annotations

import time

from src.core.config_loader import MPCfg, MPWeights
from src.data.binance_client import AggTrade, BookTicker, DepthSnapshot
from src.data.liq_buffer import Liquidation
from src.pillars.mp import (
    book_pressure,
    compose_score,
    cvd_velocity,
    evaluate_mp,
    liq_hazard,
    spread_compression,
    taker_aggression,
)


def _trades(signs: list[int], qty: float = 1.0,
            price: float = 100.0, dt_ms: int = 1000) -> list[AggTrade]:
    now = int(time.time() * 1000)
    out = []
    for i, s in enumerate(signs):
        # signed +1 means aggressor buy => is_buyer_maker False
        out.append(AggTrade(
            price=price, qty=qty,
            is_buyer_maker=(s < 0),
            timestamp_ms=now - (len(signs) - i) * dt_ms,
        ))
    return out


def _cfg(gate: float = 0.5) -> MPCfg:
    return MPCfg(
        gate=gate, hazard_min_rate=0.0,
        weights=MPWeights(
            cvd_velocity=0.25, liq_hazard=0.30,
            spread_compression=0.15, taker_aggression=0.20, book_pressure=0.10,
        ),
        bias=0.5,
    )


def test_taker_aggression_all_buys():
    trades = _trades([+1] * 10)
    val = taker_aggression(trades, window_seconds=60.0)
    assert abs(val - 1.0) < 1e-9


def test_taker_aggression_balanced():
    trades = _trades([+1, -1] * 10)
    val = taker_aggression(trades, window_seconds=60.0)
    assert abs(val) < 1e-9


def test_cvd_velocity_zero_when_few_trades():
    assert cvd_velocity(_trades([+1, +1])) == 0.0


def test_cvd_velocity_nonzero_on_sign_flip():
    # Sell-heavy first half then buy-heavy second half: CVD curves upward,
    # so 2nd derivative across start/mid/end is strictly positive.
    signs = [-1] * 30 + [+1] * 30
    val = cvd_velocity(_trades(signs, dt_ms=200), window_seconds=20.0)
    assert val > 0


def test_cvd_velocity_smaller_on_constant_flow_than_flip():
    # Constant aggressor side -> nearly-linear CVD; acceleration -> much larger.
    constant = abs(cvd_velocity(_trades([+1] * 60, dt_ms=200), window_seconds=20.0))
    flip = abs(cvd_velocity(_trades([-1] * 30 + [+1] * 30, dt_ms=200),
                            window_seconds=20.0))
    assert flip > constant * 5


def test_book_pressure_bid_heavy():
    book = BookTicker(bid_price=100.0, bid_qty=1, ask_price=100.1, ask_qty=1)
    depth = DepthSnapshot(
        bids=[(100.0, 10), (99.95, 5)],
        asks=[(100.1, 1)],
    )
    val = book_pressure(depth, mid=book.mid, band_pct=0.01)
    assert val > 0.5


def test_book_pressure_zero_total():
    book = BookTicker(bid_price=100.0, bid_qty=1, ask_price=100.1, ask_qty=1)
    depth = DepthSnapshot(bids=[(50.0, 1)], asks=[(150.0, 1)])
    assert book_pressure(depth, mid=book.mid, band_pct=0.001) == 0.0


def test_spread_compression_neutral_when_no_history():
    book = BookTicker(bid_price=100.0, bid_qty=1, ask_price=100.1, ask_qty=1)
    assert spread_compression(book, []) == 1.0


def test_spread_compression_tighter_returns_less_than_one():
    book = BookTicker(bid_price=100.0, bid_qty=1, ask_price=100.05, ask_qty=1)
    val = spread_compression(book, [0.10] * 30, half_life_samples=10.0)
    assert val < 1.0


def test_liq_hazard_empty():
    assert liq_hazard([]) == 0.0


def test_liq_hazard_increases_with_events():
    now_ms = int(time.time() * 1000)
    liqs = [
        Liquidation(symbol="BTCUSDT", side="SELL", price=100, qty=1,
                    timestamp_ms=now_ms - 1000 * i)
        for i in range(5)
    ]
    val = liq_hazard(liqs)
    assert val > 0


def test_compose_score_in_unit_interval():
    from src.pillars.mp import MPComponents
    comps = MPComponents(
        cvd_velocity=0.5, liq_hazard=4.0,
        spread_compression=0.9, taker_aggression=0.4, book_pressure=0.3,
    )
    val = compose_score(comps, _cfg())
    assert 0.0 <= val <= 1.0


def test_evaluate_mp_fails_when_low_coverage():
    book = BookTicker(bid_price=100.0, bid_qty=1, ask_price=100.1, ask_qty=1)
    depth = DepthSnapshot(bids=[(100.0, 1)], asks=[(100.1, 1)])
    res = evaluate_mp(
        trades=[], liqs=[], book=book, depth=depth, recent_spreads=[],
        cfg=_cfg(), liq_coverage_seconds=2.0,
    )
    assert not res.passed
    assert "coverage" in res.reason
