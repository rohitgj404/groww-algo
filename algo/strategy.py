"""Trend-pullback strategy for NSE cash equities.

Design goal: medium risk, long only, one position per symbol, always with a
predefined stop. Signals are computed on *closed* candles only.

Entry (all must hold):
  * EMA(fast) above EMA(slow)            -> trend is up
  * close above EMA(slow)                -> price confirms the trend
  * RSI between entry_low and entry_high -> momentum, not overextended
  * close breaks the previous candle high -> fresh push, avoids knife-catching
Exit:
  * price touches the ATR stop or target
  * trailing stop is ratcheted up once the trade is in profit
  * trend breaks (EMA fast crosses below EMA slow) or RSI is overbought
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .config import RiskPreset, Settings
from .indicators import atr, ema, rsi


@dataclass
class Candle:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class Signal:
    symbol: str
    action: str  # BUY | HOLD | EXIT
    reason: str
    price: float
    score: float = 0.0
    stop: float | None = None
    target: float | None = None
    atr: float | None = None
    indicators: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "action": self.action,
            "reason": self.reason,
            "price": round(self.price, 2) if self.price else None,
            "score": round(self.score, 1),
            "stop": round(self.stop, 2) if self.stop else None,
            "target": round(self.target, 2) if self.target else None,
            "atr": round(self.atr, 3) if self.atr else None,
            "indicators": {k: (round(v, 2) if isinstance(v, (int, float)) else v) for k, v in self.indicators.items()},
        }


def _closed(candles: list[Candle]) -> list[Candle]:
    """Drop the in-progress candle: acting on a forming candle is a classic
    source of false signals, so the engine trades the last completed one."""
    return candles[:-1] if len(candles) > 1 else candles


def compute_indicators(candles: list[Candle], settings: Settings) -> dict:
    bars = _closed(candles)
    closes = [c.close for c in bars]
    highs = [c.high for c in bars]
    lows = [c.low for c in bars]

    fast = ema(closes, settings.ema_fast)
    slow = ema(closes, settings.ema_slow)
    momentum = rsi(closes, settings.rsi_period)
    volatility = atr(highs, lows, closes, settings.atr_period)

    return {
        "bars": len(bars),
        "close": closes[-1] if closes else None,
        "prev_close": closes[-2] if len(closes) > 1 else None,
        "prev_high": highs[-2] if len(highs) > 1 else None,
        "ema_fast": fast[-1],
        "ema_slow": slow[-1],
        "ema_fast_prev": fast[-2] if len(fast) > 1 else None,
        "ema_slow_prev": slow[-2] if len(slow) > 1 else None,
        "rsi": momentum[-1],
        "atr": volatility[-1],
        "last_ts": bars[-1].ts.isoformat() if bars else None,
    }


def analyze(symbol: str, candles: list[Candle], settings: Settings) -> Signal:
    preset: RiskPreset = settings.preset
    if len(candles) < settings.min_candles:
        last = candles[-1].close if candles else 0.0
        return Signal(
            symbol=symbol,
            action="HOLD",
            reason=f"Warming up ({len(candles)}/{settings.min_candles} candles)",
            price=last,
            indicators={"bars": len(candles)},
        )

    ind = compute_indicators(candles, settings)
    price = float(ind["close"] or 0.0)
    fast, slow, rsi_value, atr_value = ind["ema_fast"], ind["ema_slow"], ind["rsi"], ind["atr"]

    if not all(isinstance(v, (int, float)) for v in (fast, slow, rsi_value, atr_value)):
        return Signal(symbol, "HOLD", "Indicators not ready", price, indicators=ind)

    trend_up = fast > slow
    above_slow = price > slow
    rsi_ok = settings.rsi_entry_low <= rsi_value <= settings.rsi_entry_high
    breakout = ind["prev_high"] is not None and price > float(ind["prev_high"])
    atr_ok = atr_value > 0 and atr_value / price < 0.06  # skip violently erratic names

    score = 0.0
    score += 30 if trend_up else 0
    score += 20 if above_slow else 0
    score += 20 if rsi_ok else 0
    score += 20 if breakout else 0
    score += 10 if atr_ok else 0

    if trend_up and above_slow and rsi_ok and breakout and atr_ok:
        stop = price - preset.atr_stop_mult * atr_value
        target = price + preset.atr_target_mult * atr_value

        # Costs are fixed per delivery round-trip, so a target that is smaller
        # than the fee drag is a guaranteed loss even when it is hit.
        target_pct = (target - price) / price * 100
        if target_pct < settings.min_edge_pct:
            return Signal(
                symbol,
                "HOLD",
                f"Target only {target_pct:.2f}% - below the {settings.min_edge_pct:.1f}% "
                f"needed to cover delivery charges",
                price,
                score=score,
                indicators=ind,
            )

        reason = (
            f"EMA{settings.ema_fast} > EMA{settings.ema_slow}, RSI {rsi_value:.0f}, "
            f"broke prior high {float(ind['prev_high']):.2f}"
        )
        return Signal(
            symbol=symbol,
            action="BUY",
            reason=reason,
            price=price,
            score=score,
            stop=round(stop, 2),
            target=round(target, 2),
            atr=atr_value,
            indicators=ind,
        )

    if not trend_up:
        reason = f"Trend down (EMA{settings.ema_fast} < EMA{settings.ema_slow})"
    elif not rsi_ok:
        reason = f"RSI {rsi_value:.0f} outside {settings.rsi_entry_low:.0f}-{settings.rsi_entry_high:.0f}"
    elif not breakout:
        reason = f"Waiting for close above {float(ind['prev_high'] or 0):.2f}"
    elif not atr_ok:
        reason = "Volatility filter (ATR too wide)"
    else:
        reason = "No setup"

    return Signal(symbol, "HOLD", reason, price, score=score, indicators=ind)


def exit_reason(position: dict, price: float, candles: list[Candle], settings: Settings) -> str | None:
    """Return a human-readable exit reason, or None to keep the position open."""
    stop = float(position["stop"])
    target = float(position["target"])

    if price <= stop:
        return "Stop loss hit"
    if price >= target:
        return "Target reached"

    ind = compute_indicators(candles, settings)
    if isinstance(ind.get("ema_fast"), (int, float)) and isinstance(ind.get("ema_slow"), (int, float)):
        if ind["ema_fast"] < ind["ema_slow"]:
            return "Trend flipped down"
    if isinstance(ind.get("rsi"), (int, float)) and ind["rsi"] >= settings.rsi_exit:
        return "RSI overbought"
    return None


def trail_stop(position: dict, price: float, settings: Settings) -> float:
    """Ratchet the stop upward once the trade has moved in our favour."""
    preset = settings.preset
    entry = float(position["entry_price"])
    atr_value = float(position.get("atr") or 0.0)
    stop = float(position["stop"])
    peak = max(float(position.get("peak") or price), price)

    if atr_value > 0 and peak - entry >= preset.trail_trigger_atr * atr_value:
        candidate = peak - preset.trail_distance_atr * atr_value
        if candidate > stop:
            stop = candidate
    return round(stop, 2)
