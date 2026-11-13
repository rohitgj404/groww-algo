"""Position sizing, charge estimates and portfolio guards."""

from __future__ import annotations

import math

from .config import RiskPreset


def estimate_charges(buy_value: float, sell_value: float) -> float:
    """Cost of a delivery round-trip on Groww (API orders).

    Brokerage min(₹20, 0.05%) per leg, STT 0.1% each side, exchange 0.00325%,
    SEBI 0.0001%, GST 18% on brokerage+exchange+SEBI, stamp 0.015% on buy and
    the ₹16.5 DP charge on the sell leg. On a small account the fixed DP fee
    dominates, which is why the bot trades rarely rather than constantly.
    """
    turnover = buy_value + sell_value
    brokerage = min(20.0, 0.0005 * buy_value) + min(20.0, 0.0005 * sell_value)
    stt = 0.001 * turnover
    exchange = 0.0000325 * turnover
    sebi = 0.000001 * turnover
    stamp = 0.00015 * buy_value
    gst = 0.18 * (brokerage + exchange + sebi)
    dp = 16.5 if sell_value > 0 else 0.0
    return round(brokerage + stt + exchange + sebi + stamp + gst + dp, 2)


def position_size(
    equity: float,
    cash: float,
    price: float,
    stop: float,
    preset: RiskPreset,
    max_price_per_share: float,
    lot_step: int = 1,
) -> tuple[int, str]:
    """Return (quantity, explanation).

    Two independent caps:
      * risk cap  - losing the full stop distance should cost <= risk_per_trade_pct
      * size cap  - notional per position <= max_position_pct of equity
    """
    if price <= 0:
        return 0, "No price"
    if max_price_per_share and price > max_price_per_share:
        return 0, f"Price {price:.2f} above per-share cap {max_price_per_share:.0f}"

    stop_distance = price - stop
    if stop_distance <= 0:
        return 0, "Invalid stop distance"

    risk_budget = equity * preset.risk_per_trade_pct / 100.0
    qty_risk = math.floor(risk_budget / stop_distance)
    notional_cap = min(equity * preset.max_position_pct, cash)
    qty_notional = math.floor(notional_cap / price)
    qty = min(qty_risk, qty_notional)

    if lot_step > 1:
        qty = (qty // lot_step) * lot_step

    if qty < 1:
        return 0, (
            f"Not enough capital: need ~{stop_distance / (preset.risk_per_trade_pct / 100.0):.0f} "
            f"equity for one share at this stop distance"
        )
    return int(qty), f"risk ₹{risk_budget:.0f} / stop ₹{stop_distance:.2f} -> {qty} share(s)"


def guard(
    *,
    open_positions: int,
    preset: RiskPreset,
    day_pnl_pct: float,
    equity: float,
    cash: float,
    price: float,
) -> str | None:
    """Portfolio-level checks before opening a new position."""
    if open_positions >= preset.max_open_positions:
        return f"Max {preset.max_open_positions} open position(s) reached"
    if day_pnl_pct <= -preset.daily_loss_limit_pct:
        return f"Daily loss limit {preset.daily_loss_limit_pct:.1f}% hit - no new entries today"
    if day_pnl_pct >= preset.daily_target_pct:
        return f"Daily target {preset.daily_target_pct:.1f}% reached - profit protected"
    if cash < price:
        return "Insufficient cash for one share"
    return None
