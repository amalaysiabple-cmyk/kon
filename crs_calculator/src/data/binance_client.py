"""Async Binance Futures public REST client (read-only).

This module talks to *public* USDT-margined futures endpoints only.
There is no signing logic on purpose — we never want to be one config
flag away from sending an order.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import aiohttp

from ..core.logger import stdlog

FAPI = "https://fapi.binance.com"
DAPI_FUTURES_DATA = "https://fapi.binance.com/futures/data"


@dataclass
class TopLongShort:
    long_account: float
    short_account: float
    long_short_ratio: float
    timestamp_ms: int


@dataclass
class Premium:
    mark_price: float
    index_price: float
    last_funding_rate: float
    next_funding_time_ms: int


@dataclass
class BookTicker:
    bid_price: float
    bid_qty: float
    ask_price: float
    ask_qty: float

    @property
    def mid(self) -> float:
        return 0.5 * (self.bid_price + self.ask_price)

    @property
    def spread(self) -> float:
        return self.ask_price - self.bid_price


@dataclass
class AggTrade:
    price: float
    qty: float
    is_buyer_maker: bool
    timestamp_ms: int

    @property
    def signed_qty(self) -> float:
        # Aggressor buy => buyer is taker => is_buyer_maker == False => +qty
        return -self.qty if self.is_buyer_maker else self.qty


@dataclass
class DepthSnapshot:
    bids: List[tuple]  # (price, qty)
    asks: List[tuple]


@dataclass
class Kline:
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float


class BinanceFuturesClient:
    """Thin async wrapper around the public Binance Futures REST surface."""

    def __init__(self, session: Optional[aiohttp.ClientSession] = None, timeout: float = 8.0):
        self._session = session
        self._owns_session = session is None
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    async def __aenter__(self) -> "BinanceFuturesClient":
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()

    async def _get(self, url: str, params: Optional[Dict[str, Any]] = None,
                   retries: int = 3) -> Any:
        assert self._session is not None, "use 'async with BinanceFuturesClient()'"
        last_err: Optional[Exception] = None
        for attempt in range(retries):
            try:
                async with self._session.get(url, params=params) as resp:
                    if resp.status >= 500:
                        raise aiohttp.ClientResponseError(
                            resp.request_info, resp.history,
                            status=resp.status, message=await resp.text(),
                        )
                    if resp.status >= 400:
                        text = await resp.text()
                        raise RuntimeError(f"binance {resp.status}: {text}")
                    return await resp.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_err = e
                wait = 0.5 * (2 ** attempt)
                stdlog.warning("binance GET %s failed (%s), retry in %.1fs", url, e, wait)
                await asyncio.sleep(wait)
        raise RuntimeError(f"binance request failed after retries: {last_err}")

    async def open_interest(self, symbol: str) -> float:
        data = await self._get(f"{FAPI}/fapi/v1/openInterest", {"symbol": symbol})
        return float(data["openInterest"])

    async def top_long_short_position_ratio(
        self, symbol: str, period: str = "5m", limit: int = 1,
    ) -> List[TopLongShort]:
        data = await self._get(
            f"{DAPI_FUTURES_DATA}/topLongShortPositionRatio",
            {"symbol": symbol, "period": period, "limit": limit},
        )
        return [
            TopLongShort(
                long_account=float(r["longAccount"]),
                short_account=float(r["shortAccount"]),
                long_short_ratio=float(r["longShortRatio"]),
                timestamp_ms=int(r["timestamp"]),
            )
            for r in data
        ]

    async def premium_index(self, symbol: str) -> Premium:
        data = await self._get(f"{FAPI}/fapi/v1/premiumIndex", {"symbol": symbol})
        return Premium(
            mark_price=float(data["markPrice"]),
            index_price=float(data["indexPrice"]),
            last_funding_rate=float(data["lastFundingRate"]),
            next_funding_time_ms=int(data["nextFundingTime"]),
        )

    async def ticker_price(self, symbol: str) -> float:
        data = await self._get(f"{FAPI}/fapi/v1/ticker/price", {"symbol": symbol})
        return float(data["price"])

    async def book_ticker(self, symbol: str) -> BookTicker:
        data = await self._get(f"{FAPI}/fapi/v1/ticker/bookTicker", {"symbol": symbol})
        return BookTicker(
            bid_price=float(data["bidPrice"]),
            bid_qty=float(data["bidQty"]),
            ask_price=float(data["askPrice"]),
            ask_qty=float(data["askQty"]),
        )

    async def agg_trades(self, symbol: str, limit: int = 100) -> List[AggTrade]:
        data = await self._get(
            f"{FAPI}/fapi/v1/aggTrades",
            {"symbol": symbol, "limit": limit},
        )
        return [
            AggTrade(
                price=float(r["p"]),
                qty=float(r["q"]),
                is_buyer_maker=bool(r["m"]),
                timestamp_ms=int(r["T"]),
            )
            for r in data
        ]

    async def depth(self, symbol: str, limit: int = 20) -> DepthSnapshot:
        data = await self._get(
            f"{FAPI}/fapi/v1/depth", {"symbol": symbol, "limit": limit},
        )
        return DepthSnapshot(
            bids=[(float(p), float(q)) for p, q in data["bids"]],
            asks=[(float(p), float(q)) for p, q in data["asks"]],
        )

    async def klines(self, symbol: str, interval: str = "15m",
                     limit: int = 100) -> List[Kline]:
        data = await self._get(
            f"{FAPI}/fapi/v1/klines",
            {"symbol": symbol, "interval": interval, "limit": limit},
        )
        return [
            Kline(
                open_time_ms=int(r[0]),
                open=float(r[1]),
                high=float(r[2]),
                low=float(r[3]),
                close=float(r[4]),
                volume=float(r[5]),
            )
            for r in data
        ]


def atr(klines: List[Kline], period: int = 14) -> float:
    """Wilder ATR. Pure function over closed klines."""
    if len(klines) < period + 1:
        return 0.0
    trs: List[float] = []
    prev_close = klines[0].close
    for k in klines[1:]:
        tr = max(
            k.high - k.low,
            abs(k.high - prev_close),
            abs(k.low - prev_close),
        )
        trs.append(tr)
        prev_close = k.close
    if len(trs) < period:
        return 0.0
    atr_val = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr_val = (atr_val * (period - 1) + tr) / period
    return atr_val


def now_ms() -> int:
    return int(time.time() * 1000)
