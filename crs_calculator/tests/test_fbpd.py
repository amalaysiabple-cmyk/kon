"""FBPD unit tests."""

from __future__ import annotations

import math
from datetime import datetime, timezone

from src.core.config_loader import FBPDCfg
from src.core.math_utils import itakura_saito_divergence
from src.pillars.fbpd import (
    annualized_funding,
    evaluate_fbpd,
    fair_value_from_basis,
    parse_quarterly_expiry,
)


def _cfg(**o) -> FBPDCfg:
    base = dict(min_divergence=0.0001, funding_filter=0.00005)
    base.update(o)
    return FBPDCfg(**base)


def test_itakura_saito_known_value():
    # IS(100, 99) ~= (100/99) - log(100/99) - 1, sign = +
    expected = (100.0 / 99.0) - math.log(100.0 / 99.0) - 1.0
    got = itakura_saito_divergence(100.0, 99.0)
    assert got > 0
    assert abs(got - expected) < 1e-9


def test_itakura_saito_zero_when_equal():
    assert itakura_saito_divergence(100.0, 100.0) == 0.0


def test_parse_quarterly_expiry_valid():
    expiry = parse_quarterly_expiry("BTCUSDT_250627")
    assert expiry == datetime(2025, 6, 27, 8, 0, 0, tzinfo=timezone.utc)


def test_parse_quarterly_expiry_invalid():
    assert parse_quarterly_expiry("BTCUSDT") is None
    assert parse_quarterly_expiry("BTCUSDT_xx0627") is None


def test_annualized_funding_zero():
    assert annualized_funding(0.0) == 0.0


def test_annualized_funding_positive():
    r = annualized_funding(0.0001)
    assert r > 0


def test_fair_value_basis_no_rate():
    fair = fair_value_from_basis(100.0, 0.0, 30.0)
    assert abs(fair - 100.0) < 1e-9


def test_evaluate_fbpd_quarterly_missing_marks_unavailable():
    res = evaluate_fbpd(
        perp_price=100.0,
        quarterly_price=None,
        funding_rate=0.0001,
        quarterly_symbol="BTCUSDT_260626",
        cfg=_cfg(),
    )
    assert not res.available
    assert not res.passed


def test_evaluate_fbpd_pass_with_divergence():
    # Use a tiny funding rate so fair ≈ quarterly; perp < quarterly => negative div.
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    res = evaluate_fbpd(
        perp_price=100.0,
        quarterly_price=100.5,
        funding_rate=1e-6,
        quarterly_symbol="BTCUSDT_260626",
        cfg=_cfg(min_divergence=1e-6, funding_filter=1e-7),
        now=now,
    )
    assert res.available
    assert res.passed, res.reason
    assert res.score < 0   # perp < fair


def test_evaluate_fbpd_fail_low_divergence():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    res = evaluate_fbpd(
        perp_price=100.0,
        quarterly_price=100.0001,
        funding_rate=1e-6,
        quarterly_symbol="BTCUSDT_260626",
        cfg=_cfg(min_divergence=0.001, funding_filter=1e-7),
        now=now,
    )
    assert res.available
    assert not res.passed
