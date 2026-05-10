"""Telegram bot interface — inline keyboard, on-demand calculation."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Dict, List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from ..core.config_loader import Config, EventEntry
from ..core.logger import JSONLLogger, stdlog
from ..data.binance_client import BinanceFuturesClient
from ..data.liq_buffer import ForceOrderStream, LiquidationBuffer
from ..decision.gate import GateEvaluator
from .cli import SUPPORTED_PAIRS
from .render import outcome_to_log_record, render_outcome


WELCOME = (
    "*CRS-FUTURES CALCULATOR*\n"
    "Chronoflux Resonance Strategy — on-demand setup calculator.\n\n"
    "Tap a pair to compute a fresh trade plan.\n"
    "/equity <USD> — set equity (default $1000)\n"
    "/status — show current state\n"
    "/help — list commands\n"
)


class TelegramApp:
    def __init__(self, token: str, config: Config, events: List[EventEntry],
                 logger: JSONLLogger):
        self.token = token
        self.config = config
        self.events = events
        self.logger = logger
        self.liq_buffer = LiquidationBuffer(
            retention_seconds=config.global_.liq_buffer_seconds,
        )
        self.stream = ForceOrderStream(self.liq_buffer)
        self.evaluator = GateEvaluator(config, events, self.liq_buffer)
        self._equity_per_chat: Dict[int, float] = {}
        self._last_calc: Dict[int, datetime] = {}

    def equity_for(self, chat_id: int) -> float:
        return self._equity_per_chat.get(
            chat_id, self.config.global_.default_equity_usd,
        )

    def _keyboard(self) -> InlineKeyboardMarkup:
        configured = [p for p in SUPPORTED_PAIRS if p in self.config.pairs]
        rows = []
        for i in range(0, len(configured), 3):
            rows.append([
                InlineKeyboardButton(p, callback_data=f"calc:{p}")
                for p in configured[i:i + 3]
            ])
        return InlineKeyboardMarkup(rows)

    async def _cmd_start(self, update: Update,
                         context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            WELCOME, parse_mode="Markdown", reply_markup=self._keyboard(),
        )

    async def _cmd_help(self, update: Update,
                        context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(WELCOME, parse_mode="Markdown")

    async def _cmd_equity(self, update: Update,
                          context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if not context.args:
            await update.message.reply_text(
                f"Current equity: ${self.equity_for(chat_id):,.2f}",
            )
            return
        try:
            value = float(context.args[0])
            if value <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "Usage: /equity <positive USD amount>",
            )
            return
        self._equity_per_chat[chat_id] = value
        await update.message.reply_text(f"Equity set to ${value:,.2f}")

    async def _cmd_status(self, update: Update,
                          context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        last = self._last_calc.get(chat_id)
        last_s = last.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") \
            if last else "never"
        await update.message.reply_text(
            f"Equity: ${self.equity_for(chat_id):,.2f}\n"
            f"Last calc: {last_s}\n"
            f"Liq buffer coverage: {self.liq_buffer.coverage_seconds():.1f}s",
        )

    async def _on_button(self, update: Update,
                         context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        data = query.data or ""
        if not data.startswith("calc:"):
            return
        pair = data.split(":", 1)[1]
        chat_id = query.message.chat_id
        equity = self.equity_for(chat_id)
        await query.message.reply_text(f"Computing {pair}...")
        try:
            async with BinanceFuturesClient() as client:
                outcome = await self.evaluator.evaluate(
                    pair, client, equity_usd=equity,
                )
        except Exception as e:  # noqa: BLE001
            await query.message.reply_text(f"API error: {e}")
            return
        text = render_outcome(outcome)
        # Telegram message limit is 4096; chunk if needed.
        for chunk in _chunk(text, 3900):
            await query.message.reply_text(f"```\n{chunk}\n```",
                                           parse_mode="Markdown")
        await query.message.reply_text("Choose another pair:",
                                       reply_markup=self._keyboard())
        self.logger.write(outcome_to_log_record(outcome))
        self._last_calc[chat_id] = datetime.now(timezone.utc)

    def build(self) -> Application:
        app = ApplicationBuilder().token(self.token).build()
        app.add_handler(CommandHandler("start", self._cmd_start))
        app.add_handler(CommandHandler("help", self._cmd_help))
        app.add_handler(CommandHandler("equity", self._cmd_equity))
        app.add_handler(CommandHandler("status", self._cmd_status))
        app.add_handler(CallbackQueryHandler(self._on_button))
        return app


def _chunk(text: str, size: int) -> List[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


async def run_telegram(config: Config, events: List[EventEntry],
                       logger: JSONLLogger) -> None:
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        raise RuntimeError(
            "TELEGRAM_TOKEN env var missing — see .env.example",
        )
    app_wrapper = TelegramApp(token, config, events, logger)
    app = app_wrapper.build()
    app_wrapper.stream.start()
    stdlog.info("Telegram bot starting...")
    try:
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        # Run until cancelled.
        import asyncio
        await asyncio.Event().wait()
    finally:
        try:
            await app.updater.stop()
        except Exception:  # noqa: BLE001
            pass
        await app.stop()
        await app.shutdown()
        await app_wrapper.stream.stop()
