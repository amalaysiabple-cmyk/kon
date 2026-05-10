"""Text rendering for CLI / Telegram output of GateOutcome."""

from __future__ import annotations

from datetime import timezone

from ..decision.gate import GateOutcome

LINE = "═══════════════════════════════════════"


def _fmt_price(value: float) -> str:
    if value >= 1000:
        return f"{value:,.2f}"
    return f"{value:,.4f}"


def render_outcome(o: GateOutcome) -> str:
    if o.valid and o.plan is not None:
        return _render_valid(o)
    return _render_invalid(o)


def _render_valid(o: GateOutcome) -> str:
    p = o.plan
    lrm, fbpd, mp, mts = o.lrm, o.fbpd, o.mp, o.mts
    ts = o.timestamp.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    slip = abs(p.entry_price - p.market_price)
    next_event = (
        f"{mts.next_event.name} "
        f"{mts.next_event.datetime_utc.strftime('%Y-%m-%d %H:%M UTC')}"
        if mts and mts.next_event else "none scheduled"
    )

    cluster_info = ""
    if lrm and lrm.cluster_price is not None:
        side = "below" if lrm.cluster_price < o.market_price else "above"
        cluster_info = (
            f" (cluster @ {_fmt_price(lrm.cluster_price)}, "
            f"${lrm.oi_usd/1e6:.1f}M OI, {lrm.distance_atr:.2f}ATR {side})"
        )

    lines = [
        LINE,
        "  CRS-FUTURES TRADE PLAN",
        LINE,
        f"Pair       : {o.pair}",
        f"Timestamp  : {ts}",
        f"Setup      : ✅ VALID",
        f"Confidence : {p.confidence:.2f}",
        "",
        f"DIRECTION  : {p.direction}",
        f"Conviction : {p.conviction:.2f}",
        "",
        "ENTRY",
        f"  Type     : {p.entry_type}",
        f"  Price    : {_fmt_price(p.entry_price)}",
        f"  Market   : {_fmt_price(p.market_price)} (slippage: ${slip:,.2f})",
        "",
        f"STOP LOSS  : {_fmt_price(p.stop_loss)} "
        f"({'-' if p.direction == 'LONG' else '+'}{p.sl_distance_pct:.2f}%)",
        "",
        "TAKE PROFIT",
        f"  TP1      : {_fmt_price(p.take_profit_1)} | 50% size  (R:R {p.rr_tp1:.1f})",
        f"  TP2      : {_fmt_price(p.take_profit_2)} | 50% size runner  (R:R {p.rr_tp2:.1f})",
        "",
        f"R:R Ratio  : {p.rr_tp1:.1f} → {p.rr_tp2:.1f}",
        f"Risk       : ${p.risk_usd_target:,.2f} target / "
        f"${p.risk_usd_actual:,.2f} actual ({p.risk_usd_actual/max(p.equity_usd,1)*100:.2f}% equity)",
        "",
        "POSITION SIZING",
        f"  Equity     : ${p.equity_usd:,.2f}",
        f"  Size USD   : ${p.size_usd:,.2f}",
        f"  Size {o.pair[:3]:<5} : {p.size_coin}",
        f"  Leverage   : {p.leverage_recommended}x → CAPPED to {p.leverage_capped}x",
        "",
        "GATE SCORES",
        f"  LRM    : {lrm.score:.2f} ✅{cluster_info}",
        f"  FBPD   : {fbpd.score:+.4f} ✅ "
        f"({'perp premium, mean revert DOWN' if fbpd.score > 0 else 'perp discount, mean revert UP'})",
        f"  MP     : {mp.score:.2f} ✅ "
        f"(CVD {mp.components.cvd_velocity:+.3f}, hazard {mp.components.liq_hazard:.1f}/min, "
        f"book {mp.components.book_pressure:+.2f})",
        f"  MTS    : {mts.state} ✅ (next event: {next_event})",
        "",
        "NOTES",
        f"  {p.notes or 'none'}",
        LINE,
        "DISCLAIMER: This is a calculator, NOT financial advice. Verify every setup manually.",
    ]
    return "\n".join(lines)


def _render_invalid(o: GateOutcome) -> str:
    ts = o.timestamp.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        LINE,
        "  CRS-FUTURES — NO SETUP",
        LINE,
        f"Pair       : {o.pair}",
        f"Timestamp  : {ts}",
        "Setup      : ❌ INVALID",
        "",
        "Failed Gates:",
    ]
    if o.lrm is not None:
        mark = "✅" if o.lrm.passed else "❌"
        lines.append(f"  LRM    : {mark} ({o.lrm.reason})")
    if o.fbpd is not None:
        if not o.fbpd.available:
            mark = "⚠"
        else:
            mark = "✅" if o.fbpd.passed else "❌"
        lines.append(f"  FBPD   : {mark} ({o.fbpd.reason})")
    if o.mp is not None:
        mark = "✅" if o.mp.passed else "❌"
        lines.append(f"  MP     : {mark} ({o.mp.reason})")
    if o.mts is not None:
        mark = "✅" if o.mts.passed else "❌"
        lines.append(f"  MTS    : {mark} ({o.mts.state}: {o.mts.reason})")

    if o.reasons:
        lines.append("")
        lines.append("Suggestion:")
        for r in o.reasons:
            lines.append(f"  - {r}")
        lines.append("Re-check in 5-10 minutes once microstructure changes.")
    lines.append(LINE)
    return "\n".join(lines)


def outcome_to_log_record(o: GateOutcome) -> dict:
    """Build the JSON-serializable dict written to calculations.jsonl."""
    rec = {
        "ts": o.timestamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pair": o.pair,
        "valid": o.valid,
        "reasons": o.reasons,
        "market_price": o.market_price,
        "direction": o.direction.direction if o.direction else None,
        "conviction": o.direction.conviction if o.direction else None,
        "confidence": o.plan.confidence if o.plan else None,
        "gates": {
            "lrm": _gate_dict(o.lrm),
            "fbpd": _gate_dict(o.fbpd),
            "mp": _gate_dict(o.mp),
            "mts": _gate_dict(o.mts),
        },
        "trade_plan": _plan_dict(o.plan),
        "raw_data_snapshot": o.raw_snapshot,
    }
    return rec


def _gate_dict(gate) -> dict | None:
    if gate is None:
        return None
    if hasattr(gate, "components"):  # MP
        return {
            "score": gate.score,
            "passed": gate.passed,
            "reason": gate.reason,
            "components": gate.components.__dict__,
        }
    return {k: v for k, v in gate.__dict__.items()}


def _plan_dict(plan) -> dict | None:
    if plan is None:
        return None
    return plan.__dict__
