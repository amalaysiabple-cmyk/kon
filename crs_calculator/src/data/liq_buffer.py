"""60s rolling liquidation buffer fed by Binance WS forceOrder stream.

The buffer is intentionally small and self-healing — if the socket drops we
reconnect, but the consumer must check `coverage_seconds()` before relying on
the data, since a freshly reconnected buffer has nothing in it.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional

import websockets

from ..core.logger import stdlog


@dataclass
class Liquidation:
    symbol: str
    side: str            # "BUY" or "SELL" (side of the liquidation order)
    price: float
    qty: float
    timestamp_ms: int

    @property
    def notional_usd(self) -> float:
        return self.price * self.qty


class LiquidationBuffer:
    """Per-symbol rolling buffer with bounded retention."""

    def __init__(self, retention_seconds: float = 60.0):
        self.retention_seconds = retention_seconds
        self._buffers: Dict[str, Deque[Liquidation]] = {}
        self._first_event_ms: Optional[int] = None

    def add(self, ev: Liquidation) -> None:
        buf = self._buffers.setdefault(ev.symbol, deque())
        buf.append(ev)
        if self._first_event_ms is None:
            self._first_event_ms = ev.timestamp_ms
        self._evict(ev.symbol, now_ms=ev.timestamp_ms)

    def _evict(self, symbol: str, now_ms: int) -> None:
        buf = self._buffers.get(symbol)
        if buf is None:
            return
        cutoff = now_ms - int(self.retention_seconds * 1000)
        while buf and buf[0].timestamp_ms < cutoff:
            buf.popleft()

    def snapshot(self, symbol: str) -> list[Liquidation]:
        now = int(time.time() * 1000)
        self._evict(symbol, now_ms=now)
        return list(self._buffers.get(symbol, deque()))

    def coverage_seconds(self) -> float:
        if self._first_event_ms is None:
            return 0.0
        elapsed = (time.time() * 1000) - self._first_event_ms
        return min(elapsed / 1000.0, self.retention_seconds)


class ForceOrderStream:
    """Background task that pumps !forceOrder@arr into a LiquidationBuffer."""

    URL = "wss://fstream.binance.com/ws/!forceOrder@arr"

    def __init__(self, buffer: LiquidationBuffer):
        self.buffer = buffer
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    async def _consume_one(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
            order = payload.get("o") or {}
            ev = Liquidation(
                symbol=str(order.get("s", "")),
                side=str(order.get("S", "")),
                price=float(order.get("p", 0.0) or 0.0),
                qty=float(order.get("q", 0.0) or 0.0),
                timestamp_ms=int(payload.get("E", int(time.time() * 1000))),
            )
            if ev.symbol:
                self.buffer.add(ev)
        except (ValueError, KeyError, TypeError) as e:
            stdlog.debug("forceOrder parse error: %s (%s)", e, raw[:200])

    async def _run_once(self) -> None:
        async with websockets.connect(self.URL, ping_interval=20, ping_timeout=20) as ws:
            stdlog.info("forceOrder WS connected")
            async for raw in ws:
                if self._stop.is_set():
                    return
                await self._consume_one(raw)

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._run_once()
                backoff = 1.0
            except Exception as e:  # noqa: BLE001
                stdlog.warning("forceOrder WS error %s; reconnect in %.1fs", e, backoff)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2.0, 30.0)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name="forceOrderStream")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
