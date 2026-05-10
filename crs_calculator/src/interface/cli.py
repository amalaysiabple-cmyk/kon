"""Interactive CLI menu."""

from __future__ import annotations

import asyncio
from typing import List, Optional

from rich.console import Console
from rich.panel import Panel

from ..core.config_loader import Config, EventEntry
from ..core.logger import JSONLLogger, stdlog
from ..data.binance_client import BinanceFuturesClient
from ..data.liq_buffer import ForceOrderStream, LiquidationBuffer
from ..decision.gate import GateEvaluator
from .render import outcome_to_log_record, render_outcome


SUPPORTED_PAIRS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "TRXUSDT", "DOGEUSDT", "ADAUSDT"]


class CLI:
    def __init__(self, config: Config, events: List[EventEntry], logger: JSONLLogger):
        self.config = config
        self.events = events
        self.logger = logger
        self.console = Console()
        self.equity = config.global_.default_equity_usd
        self.liq_buffer = LiquidationBuffer(
            retention_seconds=config.global_.liq_buffer_seconds,
        )
        self.stream = ForceOrderStream(self.liq_buffer)
        self.evaluator = GateEvaluator(config, events, self.liq_buffer)

    def _menu(self) -> str:
        configured = [p for p in SUPPORTED_PAIRS if p in self.config.pairs]
        self.console.print(Panel.fit(
            "[bold cyan]CRS-FUTURES CALCULATOR[/bold cyan]\n"
            "Chronoflux Resonance Strategy",
            border_style="cyan",
        ))
        self.console.print("\n[bold]Select pair:[/bold]")
        for i, p in enumerate(configured, start=1):
            self.console.print(f"  [{i}] {p}")
        self.console.print(f"  [A] All configured pairs (sequential)")
        self.console.print(f"  [E] Set equity (current: ${self.equity:,.2f})")
        self.console.print(f"  [Q] Quit")
        choice = input("\n> ").strip().upper()
        return choice

    async def _run_pair(self, pair: str) -> None:
        self.console.print(f"\n[dim]Fetching data for {pair}...[/dim]")
        try:
            async with BinanceFuturesClient() as client:
                outcome = await self.evaluator.evaluate(
                    pair, client, equity_usd=self.equity,
                )
        except Exception as e:  # noqa: BLE001
            self.console.print(f"[red]API error: {e}[/red]")
            return
        text = render_outcome(outcome)
        self.console.print(text)
        self.logger.write(outcome_to_log_record(outcome))

    async def run(self) -> None:
        self.stream.start()
        try:
            while True:
                choice = self._menu()
                configured = [p for p in SUPPORTED_PAIRS if p in self.config.pairs]
                if choice == "Q":
                    break
                if choice == "E":
                    raw = input("New equity USD: ").strip()
                    try:
                        self.equity = float(raw)
                    except ValueError:
                        self.console.print("[red]invalid number[/red]")
                    continue
                if choice == "A":
                    for p in configured:
                        await self._run_pair(p)
                    input("\nPress Enter for menu...")
                    continue
                idx: Optional[int] = None
                try:
                    idx = int(choice) - 1
                except ValueError:
                    self.console.print("[red]invalid choice[/red]")
                    continue
                if idx is None or idx < 0 or idx >= len(configured):
                    self.console.print("[red]out of range[/red]")
                    continue
                await self._run_pair(configured[idx])
                input("\nPress Enter for menu...")
        finally:
            await self.stream.stop()


async def run_cli(config: Config, events: List[EventEntry],
                  logger: JSONLLogger) -> None:
    cli = CLI(config, events, logger)
    try:
        await cli.run()
    except (KeyboardInterrupt, EOFError):
        stdlog.info("CLI exit")
