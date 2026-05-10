"""Pillar 2 — Funding-Basis Phase Drift.

Detects when the perpetual contract has drifted away from the basis-implied
fair value derived from the nearest quarterly futures contract.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from ..core.config_loader import FBPDCfg
from ..core.math_utils import itakura_saito_divergence


FUNDING_PERIODS_PER_YEAR = 365.0 * 3.0  # funding settles every 8h => 3/day


@dataclass
class FBPDResult:
    passed: bool
    score: float                  # signed Itakura-Saito divergence
    perp_price: float
    fair_price: Optional[float]
    funding_rate: float
    days_to_expiry: Optional[float]
    available: bool               # False when quarterly missing => N/A
    reason: str


def parse_quarterly_expiry(symbol: str) -> Optional[datetime]:
    """Parse the YYMMDD suffix of a Binance quarterly symbol like
    'BTCUSDT_250627' -> 2025-06-27 08:00 UTC (Binance settlement time).
    """
    if "_" not in symbol:
        return None
    suffix = symbol.split("_")[-1]
    if len(suffix) != 6 or not suffix.isdigit():
        return None
    yy, mm, dd = int(suffix[0:2]), int(suffix[2:4]), int(suffix[4:6])
    year = 2000 + yy
    try:
        return datetime(year, mm, dd, 8, 0, 0, tzinfo=timezone.utc)
    except ValueError:
        return None


def annualized_funding(funding_rate: float) -> float:
    """Convert per-period funding rate (8h) to a continuously-compounded annual rate."""
    if abs(funding_rate) < 1e-12:
        return 0.0
    growth = (1.0 + funding_rate) ** FUNDING_PERIODS_PER_YEAR
    if growth <= 0:
        return 0.0
    return math.log(growth)


def fair_value_from_basis(quarterly_price: float, annual_rate: float,
                          days_to_expiry: float) -> float:
    """Implied perp fair value = quarterly * exp(-r * T)."""
    t_years = max(days_to_expiry, 0.0) / 365.0
    return quarterly_price * math.exp(-annual_rate * t_years)


def evaluate_fbpd(
    perp_price: float,
    quarterly_price: Optional[float],
    funding_rate: float,
    quarterly_symbol: str,
    cfg: FBPDCfg,
    now: Optional[datetime] = None,
) -> FBPDResult:
    """Compute the FBPD divergence and gate decision.

    If `quarterly_price` is None or expiry can't be parsed, the gate is
    marked unavailable (N/A): not a pass, not a fail — caller decides.
    """
    if perp_price <= 0:
        return FBPDResult(False, 0.0, perp_price, None, funding_rate, None, False,
                          "perp price invalid")

    if quarterly_price is None or quarterly_price <= 0:
        return FBPDResult(False, 0.0, perp_price, None, funding_rate, None, False,
                          "quarterly price unavailable")

    expiry = parse_quarterly_expiry(quarterly_symbol)
    if expiry is None:
        return FBPDResult(False, 0.0, perp_price, None, funding_rate, None, False,
                          f"cannot parse quarterly symbol {quarterly_symbol}")

    now_dt = now or datetime.now(timezone.utc)
    days_to_expiry = (expiry - now_dt).total_seconds() / 86400.0
    if days_to_expiry <= 0:
        return FBPDResult(False, 0.0, perp_price, None, funding_rate,
                          days_to_expiry, False,
                          f"quarterly expired ({quarterly_symbol})")

    fair = fair_value_from_basis(
        quarterly_price, annualized_funding(funding_rate), days_to_expiry,
    )
    if fair <= 0:
        return FBPDResult(False, 0.0, perp_price, None, funding_rate,
                          days_to_expiry, False, "fair value <= 0")

    div = itakura_saito_divergence(perp_price, fair)

    reasons = []
    passed = True
    if abs(div) < cfg.min_divergence:
        passed = False
        reasons.append(f"|div| {abs(div):.5f} < {cfg.min_divergence:.5f}")
    if abs(funding_rate) < cfg.funding_filter:
        passed = False
        reasons.append(
            f"|funding| {abs(funding_rate):.5f} < {cfg.funding_filter:.5f}",
        )

    return FBPDResult(
        passed=passed,
        score=div,
        perp_price=perp_price,
        fair_price=fair,
        funding_rate=funding_rate,
        days_to_expiry=days_to_expiry,
        available=True,
        reason="; ".join(reasons) if reasons else "ok",
    )
