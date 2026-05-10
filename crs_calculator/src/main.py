"""CLI entry point: dispatch to CLI / Telegram / hybrid mode."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from dotenv import load_dotenv

from .core.config_loader import load_config, load_events
from .core.logger import JSONLLogger, stdlog
from .interface.cli import run_cli


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="crs_calculator",
        description="CRS-Futures on-demand trade calculator (read-only).",
    )
    p.add_argument("--mode", choices=["cli", "telegram", "both"], default="cli")
    p.add_argument("--config", default="config/thresholds.yaml")
    p.add_argument("--events", default="config/events.yaml")
    return p.parse_args()


async def _both(config, events, logger) -> None:
    from .interface.telegram_bot import run_telegram
    await asyncio.gather(
        run_cli(config, events, logger),
        run_telegram(config, events, logger),
    )


def main() -> None:
    load_dotenv()
    args = parse_args()
    base = Path(__file__).resolve().parent.parent
    config_path = (base / args.config) if not Path(args.config).is_absolute() \
        else Path(args.config)
    events_path = (base / args.events) if not Path(args.events).is_absolute() \
        else Path(args.events)

    config = load_config(config_path)
    events = load_events(events_path).events
    log_path = config.global_.log_path
    if not Path(log_path).is_absolute():
        log_path = str(base / log_path)
    logger = JSONLLogger(log_path)

    stdlog.info("CRS calculator starting in %s mode (config=%s)",
                args.mode, config_path)

    if args.mode == "cli":
        asyncio.run(run_cli(config, events, logger))
    elif args.mode == "telegram":
        from .interface.telegram_bot import run_telegram
        asyncio.run(run_telegram(config, events, logger))
    else:
        asyncio.run(_both(config, events, logger))


if __name__ == "__main__":
    main()
