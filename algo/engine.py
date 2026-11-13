"""The trading engine: one background thread, one decision loop, many guards.

The engine owns the account state (cash, open positions), asks the strategy for
signals on closed candles, sizes each trade with the risk module, and sends
orders to the active executor (paper or Groww). Every state change is persisted
so a restart resumes cleanly and the dashboard can replay history.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date

from .broker import BrokerError, build_executor
from .config import PRESETS, Settings
from .datafeed import DataError, Feed, build_feed
from .market import can_enter_now, clock_label, is_market_open, market_phase, now_ist, session_date
from .risk import estimate_charges, guard, position_size
from .screener import MarketScreener
from .storage import Store
from .strategy import Candle, analyze, exit_reason, trail_stop

log = logging.getLogger("growwalgo.engine")


class TradingEngine:
    def __init__(self, settings: Settings | None = None, store: Store | None = None) -> None:
        self.settings = settings or Settings()
        self.store = store or Store()
        self.lock = threading.RLock()

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._paused = False
        self._state = "stopped"  # stopped | running | paused
        self._last_error: str | None = None
        self._last_log: dict[str, float] = {}

        self.feed: Feed = build_feed(self.settings)
        self.mode = self.settings.mode if self.settings.mode in {"paper", "live"} else "paper"
        self.executor, executor_error = build_executor(self.settings, self.mode)
        if self.mode == "live" and self.executor.kind != "groww":
            self.mode = "paper"

        self.cash = self._initial_cash()
        # Only resume positions belonging to the mode we are starting in.
        self.positions: dict[str, dict] = {
            p["symbol"]: p for p in self.store.open_positions() if p.get("mode", self.mode) == self.mode
        }
        self.holdings: list[dict] = []
        if self.mode == "live":
            self.holdings = self.executor.holdings()
        self._last_holdings_refresh = time.time()
        self.prices: dict[str, float] = {}
        self.signals: dict[str, dict] = {}
        self._candle_cache: dict[str, tuple[float, list[Candle]]] = {}
        self._cooldown: dict[str, float] = {}
        self._scanning = False
        self._last_scan_check = 0.0
        self.screener = MarketScreener(self.settings, self.feed, self.store)
        self._day = session_date()
        self._day_start_equity = self._load_day_start_equity()
        self._halted_reason: str | None = None

        if executor_error:
            self.store.add_event("warn", executor_error)
        self.store.add_event(
            "info",
            f"Engine initialised in {self.mode.upper()} mode | prices: {self.feed.kind} | "
            f"orders: {self.executor.kind}",
        )

    # ------------------------------------------------------------------ setup
    def _initial_cash(self) -> float:
        if self.mode == "live":
            live_cash = self.executor.cash()
            if live_cash is not None:
                return live_cash
            return float(self.store.get_kv("live_cash", str(self.settings.capital)))
        return float(self.store.get_kv("paper_cash", str(self.settings.capital)))

    def _load_day_start_equity(self) -> float:
        stored = self.store.get_json("day_start_equity", None)
        if stored and stored.get("date") == self._day.isoformat():
            return float(stored.get("equity", self.settings.capital))
        return float(self._equity())

    def _save_day_start_equity(self, equity: float) -> None:
        self.store.set_json("day_start_equity", {"date": self._day.isoformat(), "equity": round(equity, 2)})

    # ---------------------------------------------------------------- helpers
    def _log_once(self, key: str, level: str, message: str, every: float = 120.0) -> None:
        now = time.time()
        if now - self._last_log.get(key, 0) >= every:
            self._last_log[key] = now
            self.store.add_event(level, message)
            log.info("%s: %s", key, message)

    def _invested(self) -> float:
        total = 0.0
        for position in self.positions.values():
            price = self.prices.get(position["symbol"], position["entry_price"])
            total += price * position["qty"]
        return total

    def _holdings_value(self) -> float:
        """Market value of the user's pre-existing Groww holdings.

        These were in the account before the bot and are never traded by it, but
        they are part of account equity, so leaving them out would understate it.
        """
        total = 0.0
        for holding in self.holdings:
            price = self.prices.get(holding["symbol"], holding.get("average_price", 0.0))
            total += price * holding["qty"]
        return total

    def _equity(self) -> float:
        return round(self.cash + self._invested() + self._holdings_value(), 2)

    def _day_pnl_pct(self, equity: float | None = None) -> float:
        base = self._day_start_equity or self.settings.capital
        if base <= 0:
            return 0.0
        return (equity if equity is not None else self._equity()) / base * 100.0 - 100.0

    def _roll_day(self) -> None:
        today = session_date()
        if today != self._day:
            equity = self._equity()
            self._day = today
            self._day_start_equity = equity
            self._save_day_start_equity(equity)
            self._halted_reason = None
            self.store.add_event("info", f"New trading day {today} - equity carried forward at ₹{equity:,.2f}")

    def _market_tradable(self) -> bool:
        """Simulated data trades around the clock so the app stays demoable."""
        if self.feed.kind == "simulated":
            return True
        return is_market_open()

    def _candles(self, symbol: str) -> list[Candle]:
        cached = self._candle_cache.get(symbol)
        now = time.time()
        if cached and now - cached[0] < self.settings.candle_refresh_seconds:
            return cached[1]
        try:
            candles = self.feed.candles(symbol, self.settings)
        except DataError as exc:
            self._log_once(f"candles-{symbol}", "error", f"{symbol}: candle fetch failed - {exc}", 300)
            return cached[1] if cached else []
        if candles:
            self._candle_cache[symbol] = (now, candles)
        return candles

    # -------------------------------------------------------------- lifecycle
    def start(self) -> dict:
        with self.lock:
            if self._thread and self._thread.is_alive():
                self._paused = False
                self._state = "running"
                return {"ok": True, "message": "Engine already running"}
            self._stop.clear()
            self._paused = False
            self._state = "running"
            self._thread = threading.Thread(target=self._run, name="trading-engine", daemon=True)
            self._thread.start()
        self.store.add_event("info", f"Engine started in {self.mode.upper()} mode")
        self._maybe_scan()
        return {"ok": True, "message": "Engine started"}

    def pause(self) -> dict:
        with self.lock:
            self._paused = True
            if self._state == "running":
                self._state = "paused"
        self.store.add_event("warn", "Engine paused - existing positions remain protected by their stops")
        return {"ok": True, "message": "Engine paused"}

    def resume(self) -> dict:
        with self.lock:
            if not self._thread or not self._thread.is_alive():
                return self.start()
            self._paused = False
            self._state = "running"
        self.store.add_event("info", "Engine resumed")
        return {"ok": True, "message": "Engine resumed"}

    def stop(self) -> dict:
        with self.lock:
            self._stop.set()
            thread = self._thread
            self._state = "stopped"
            self._paused = False
        if thread and thread.is_alive():
            thread.join(timeout=8)
        self.store.add_event("info", "Engine stopped")
        return {"ok": True, "message": "Engine stopped"}

    # ------------------------------------------------------------------ scan
    def _maybe_scan(self) -> None:
        """Refresh the watchlist when the last full-market scan is stale."""
        now = time.time()
        if not self.settings.auto_screen or now - self._last_scan_check < 600:
            return
        self._last_scan_check = now
        last = self.store.get_json("last_scan", None)
        if last and now - float(last.get("epoch", 0)) < self.settings.scan_refresh_hours * 3600:
            return
        self.start_scan()

    def start_scan(self) -> dict:
        with self.lock:
            if self._scanning:
                return {"ok": False, "message": "A market scan is already running"}
            self._scanning = True
        threading.Thread(target=self._scan_worker, name="market-scan", daemon=True).start()
        self.store.add_event("info", "Scanning the full NSE catalogue for tradeable uptrends…")
        return {"ok": True, "message": "Market scan started"}

    def _scan_worker(self) -> None:
        try:
            summary = self.screener.run(self._equity(), self.cash, self.settings.scan_size)
            selected = summary.get("selected") or []
            if summary.get("ok") and selected:
                with self.lock:
                    self.settings.universe = selected
                    self.store.set_json("universe", selected)
                    self.signals.clear()
                self.store.add_event(
                    "info", f"{summary['message']} ({summary.get('seconds', 0)}s)"
                )
            else:
                self.store.add_event("warn", summary.get("message", "Market scan produced no names"))
        except Exception as exc:  # noqa: BLE001 - a scan must never kill the engine
            self.store.add_event("error", f"Market scan failed: {exc}")
        finally:
            with self.lock:
                self._scanning = False

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.time()
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - keep the loop alive, surface the error
                self._last_error = str(exc)
                log.exception("tick failed")
                self.store.add_event("error", f"Engine error: {exc}")
            elapsed = time.time() - started
            self._stop.wait(max(1.0, self.settings.poll_seconds - elapsed))

    # ------------------------------------------------------------------- tick
    def tick(self) -> None:
        with self.lock:
            self._roll_day()
            watch = sorted(
                set(self.positions)
                | set(self.settings.universe)
                | {h["symbol"] for h in self.holdings}
            )
            try:
                quotes = self.feed.ltp(watch)
                self._last_error = None
            except DataError as exc:
                self._last_error = str(exc)
                self._log_once("quotes", "error", f"Quote fetch failed: {exc}", 180)
                quotes = {}
            self.prices.update(quotes)

            if self.mode == "live" and int(time.time()) % 30 == 0:
                live_cash = self.executor.cash()
                if live_cash is not None:
                    self.cash = live_cash
                if time.time() - self._last_holdings_refresh > 300:
                    self._last_holdings_refresh = time.time()
                    self.holdings = self.executor.holdings()

            self._manage_positions()
            if not self._paused and self._market_tradable():
                self._scan_entries()
            self._refresh_signals()
            self._maybe_scan()

    def _manage_positions(self) -> None:
        for symbol in list(self.positions):
            position = self.positions[symbol]
            price = self.prices.get(symbol)
            if not price:
                continue

            position["peak"] = max(float(position.get("peak") or price), price)
            candles = self._candle_cache.get(symbol, (0, []))[1] or self._candles(symbol)

            new_stop = trail_stop(position, price, self.settings)
            if new_stop > float(position["stop"]):
                position["stop"] = new_stop
                self.store.save_position(position)
                self.store.add_event(
                    "info", f"{symbol}: stop trailed up to ₹{new_stop:.2f} (price ₹{price:.2f})"
                )

            reason = exit_reason(position, price, candles, self.settings) if candles else None
            if reason is None:
                if price <= position["stop"]:
                    reason = "Stop loss hit"
                elif price >= position["target"]:
                    reason = "Target reached"

            if reason and self._market_tradable():
                self._close_position(symbol, price, reason)

    def _scan_entries(self) -> None:
        preset = self.settings.preset
        equity = self._equity()
        day_pnl = self._day_pnl_pct(equity)

        if not can_enter_now(self.settings.no_new_entries_after) and self.feed.kind != "simulated":
            self._log_once("entries-closed", "info", "Inside market hours but past the new-entry cutoff", 1800)
            return

        if day_pnl <= -preset.daily_loss_limit_pct and not self._halted_reason:
            self._halted_reason = f"Daily loss limit {preset.daily_loss_limit_pct:.1f}% hit"
            self.store.add_event("warn", f"{self._halted_reason} - no new entries until tomorrow")
            return
        if day_pnl >= preset.daily_target_pct and not self._halted_reason:
            self._halted_reason = f"Daily target {preset.daily_target_pct:.1f}% reached"
            self.store.add_event("info", f"{self._halted_reason} - profit protected, no new entries")
            return

        for symbol in self.settings.universe:
            if len(self.positions) >= preset.max_open_positions:
                break
            if symbol in self.positions:
                continue
            if time.time() < self._cooldown.get(symbol, 0.0):
                continue
            price = self.prices.get(symbol)
            if not price:
                continue

            candles = self._candles(symbol)
            if len(candles) < self.settings.min_candles:
                continue

            signal = analyze(symbol, candles, self.settings)
            self.store.set_json(f"signal:{symbol}", signal.to_dict())

            if signal.action != "BUY" or not signal.stop:
                continue

            blocked = guard(
                open_positions=len(self.positions),
                preset=preset,
                day_pnl_pct=day_pnl,
                equity=equity,
                cash=self.cash,
                price=price,
            )
            if blocked:
                self._log_once(f"guard-{symbol}", "info", f"{symbol}: skipped - {blocked}", 900)
                continue

            qty, sizing_note = position_size(
                equity=equity,
                cash=self.cash,
                price=signal.price,
                stop=signal.stop,
                preset=preset,
                max_price_per_share=self.settings.max_price_per_share,
            )
            if qty < 1:
                self._log_once(f"size-{symbol}", "info", f"{symbol}: {sizing_note}", 1800)
                continue

            try:
                # Send the order at the live price; the stop and target are
                # re-based on the actual fill so slippage cannot widen risk.
                fill = self.executor.place("BUY", symbol, qty, price, self.settings)
            except BrokerError as exc:
                self.store.add_event("error", f"{symbol}: BUY rejected - {exc}")
                continue

            notional = fill.price * fill.qty
            if notional > self.cash + 0.01:
                self.store.add_event(
                    "warn", f"{symbol}: order would exceed available cash (₹{notional:,.2f}) - ignored"
                )
                try:
                    self.executor.place("SELL", symbol, qty, fill.price, self.settings)
                except BrokerError:
                    pass
                continue

            self.cash = round(self.cash - notional, 2)
            self._store_cash()

            atr_value = float(signal.atr or 0.0)
            stop = fill.price - preset.atr_stop_mult * atr_value if atr_value else float(signal.stop)
            target = fill.price + preset.atr_target_mult * atr_value if atr_value else float(signal.target or fill.price)

            position = {
                "symbol": symbol,
                "qty": int(fill.qty),
                "entry_price": round(fill.price, 2),
                "stop": round(max(stop, 0.01), 2),
                "target": round(target, 2),
                "atr": round(atr_value, 3),
                "peak": round(fill.price, 2),
                "opened_at": now_ist().strftime("%Y-%m-%d %H:%M:%S"),
                "mode": self.mode,
                "reason": signal.reason,
            }
            self.positions[symbol] = position
            self.store.save_position(position)
            self.store.add_trade(
                symbol=symbol,
                side="BUY",
                qty=fill.qty,
                price=fill.price,
                reason=signal.reason,
                mode=self.mode,
            )
            self.store.add_event(
                "trade",
                f"BUY {fill.qty} x {symbol} @ ₹{fill.price:,.2f} "
                f"(stop ₹{position['stop']:,.2f}, target ₹{position['target']:,.2f}) - {sizing_note}",
            )

    def _close_position(self, symbol: str, price: float, reason: str) -> None:
        position = self.positions.get(symbol)
        if not position:
            return
        qty = int(position["qty"])
        try:
            fill = self.executor.place("SELL", symbol, qty, price, self.settings)
        except BrokerError as exc:
            self.store.add_event("error", f"{symbol}: SELL failed - {exc}")
            return

        entry = float(position["entry_price"])
        charges = estimate_charges(entry * qty, fill.price * qty)
        gross = (fill.price - entry) * qty
        pnl = round(gross - charges, 2)

        self.cash = round(self.cash + fill.price * qty - charges, 2)
        self._store_cash()
        del self.positions[symbol]
        self._cooldown[symbol] = time.time() + self.settings.cooldown_minutes * 60
        self.store.delete_position(symbol)
        self.store.add_trade(
            symbol=symbol,
            side="SELL",
            qty=qty,
            price=fill.price,
            reason=reason,
            pnl=pnl,
            charges=charges,
            mode=self.mode,
        )
        sign = "+" if pnl >= 0 else ""
        self.store.add_event(
            "trade",
            f"SELL {qty} x {symbol} @ ₹{fill.price:,.2f} ({reason}) - P&L {sign}₹{pnl:,.2f}",
        )

    def _store_cash(self) -> None:
        key = "live_cash" if self.mode == "live" else "paper_cash"
        self.store.set_kv(key, str(round(self.cash, 2)))

    def _refresh_signals(self) -> None:
        for symbol in self.settings.universe:
            cached = self._candle_cache.get(symbol)
            if not cached:
                continue
            signal = analyze(symbol, cached[1], self.settings)
            self.signals[symbol] = signal.to_dict()

    # -------------------------------------------------------------- controls
    def close_all(self) -> dict:
        with self.lock:
            if not self.positions:
                return {"ok": True, "message": "No open positions"}
            if not self._market_tradable():
                return {"ok": False, "message": "Market is closed - cannot square off now"}
            for symbol in list(self.positions):
                price = self.prices.get(symbol) or self.positions[symbol]["entry_price"]
                self._close_position(symbol, price, "Manual square-off")
        return {"ok": True, "message": "All positions squared off"}

    def reset_paper(self) -> dict:
        with self.lock:
            if self._thread and self._thread.is_alive():
                return {"ok": False, "message": "Stop the engine before resetting the paper account"}
            if self.positions:
                return {"ok": False, "message": "Close all positions before resetting"}
            self.store.reset_paper()
            self.cash = float(self.settings.capital)
            self.store.set_kv("paper_cash", str(self.cash))
            self._day_start_equity = self.cash
            self._save_day_start_equity(self.cash)
            self._candle_cache.clear()
            self.signals.clear()
            self._halted_reason = None
            self.store.add_event("info", f"Paper account reset to ₹{self.cash:,.2f}")
        return {"ok": True, "message": "Paper account reset"}

    def switch_mode(self, mode: str) -> dict:
        mode = mode.strip().lower()
        if mode not in {"paper", "live"}:
            return {"ok": False, "message": "Mode must be paper or live"}
        with self.lock:
            if self._thread and self._thread.is_alive():
                return {"ok": False, "message": "Stop the engine before switching mode"}
            if mode == self.mode:
                return {"ok": True, "message": f"Already in {mode} mode"}
            if mode == "live" and not self.settings.has_credentials:
                return {"ok": False, "message": "Live mode needs Groww API credentials in .env"}
            executor, error = build_executor(self.settings, mode)
            if mode == "live" and executor.kind != "groww":
                return {"ok": False, "message": error or "Groww connection failed"}

            self.positions.clear()
            self.store.clear_positions()
            self.mode = mode
            self.executor = executor
            live_cash = executor.cash() if mode == "live" else None
            if live_cash is not None:
                self.cash = live_cash
            self.holdings = executor.holdings() if mode == "live" else []
            self._last_holdings_refresh = time.time()
            self._store_cash()
            self.store.set_kv("mode", mode)
            self._day_start_equity = self._equity()
            self._save_day_start_equity(self._day_start_equity)
            self._candle_cache.clear()
            self.signals.clear()
            self._halted_reason = None
            self.store.add_event("warn", f"Switched to {mode.upper()} mode")
        return {"ok": True, "message": f"Now in {mode} mode"}

    def update_settings(self, payload: dict) -> dict:
        with self.lock:
            if "risk_level" in payload:
                level = str(payload["risk_level"]).lower()
                if level not in PRESETS:
                    return {"ok": False, "message": f"Unknown risk level {level}"}
                self.settings.risk_level = level
                self.store.set_kv("risk_level", level)
                self.store.add_event("info", f"Risk profile set to {PRESETS[level].label}")
            if "universe" in payload and isinstance(payload["universe"], list):
                cleaned = [str(s).strip().upper() for s in payload["universe"] if str(s).strip()]
                cleaned = [s for s in cleaned if s.isalnum() or "-" in s][:20]
                if cleaned:
                    self.settings.universe = cleaned
                    self.store.set_json("universe", cleaned)
                    self.store.add_event("info", f"Watchlist updated: {', '.join(cleaned)}")
            if "capital" in payload and self.mode == "paper":
                if self._thread and self._thread.is_alive():
                    return {"ok": False, "message": "Stop the engine to change starting capital"}
                try:
                    capital = float(payload["capital"])
                except (TypeError, ValueError):
                    return {"ok": False, "message": "Capital must be a number"}
                if capital <= 0:
                    return {"ok": False, "message": "Capital must be positive"}
                self.settings.capital = capital
                self.cash = capital
                self.store.set_kv("paper_cash", str(capital))
                self._day_start_equity = self._equity()
                self._save_day_start_equity(self._day_start_equity)
                self.store.add_event("info", f"Paper capital set to ₹{capital:,.2f}")
        return {"ok": True, "message": "Settings applied"}

    # -------------------------------------------------------------- snapshot
    def snapshot(self) -> dict:
        with self.lock:
            equity = self._equity()
            invested = self._invested()
            day_pnl = equity - self._day_start_equity
            day_pnl_pct = self._day_pnl_pct(equity)

            positions = []
            unrealised = 0.0
            for position in self.positions.values():
                price = self.prices.get(position["symbol"], float(position["entry_price"]))
                value = price * int(position["qty"])
                cost = float(position["entry_price"]) * int(position["qty"])
                pnl = value - cost
                unrealised += pnl
                positions.append(
                    {
                        "symbol": position["symbol"],
                        "qty": int(position["qty"]),
                        "entry_price": round(float(position["entry_price"]), 2),
                        "ltp": round(price, 2),
                        "value": round(value, 2),
                        "pnl": round(pnl, 2),
                        "pnl_pct": round((price / float(position["entry_price"]) - 1) * 100, 2),
                        "stop": round(float(position["stop"]), 2),
                        "target": round(float(position["target"]), 2),
                        "opened_at": position["opened_at"],
                        "reason": position.get("reason", ""),
                    }
                )

            signals = []
            for symbol in self.settings.universe:
                cached = self._candle_cache.get(symbol)
                signal = self.signals.get(symbol) or (analyze(symbol, cached[1], self.settings).to_dict() if cached else None)
                if signal:
                    signal = dict(signal)
                    signal["ltp"] = round(self.prices.get(symbol, signal.get("price") or 0), 2)
                    signals.append(signal)
                else:
                    signals.append(
                        {"symbol": symbol, "action": "HOLD", "reason": "Loading candles", "ltp": self.prices.get(symbol)}
                    )

            preset = self.settings.preset
            return {
                "status": {
                    "state": self._state,
                    "paused": self._paused,
                    "mode": self.mode,
                    "feed": self.feed.kind,
                    "execution": self.executor.kind,
                    "simulated": self.feed.kind == "simulated",
                    "market": market_phase(),
                    "market_open": is_market_open(),
                    "entries_allowed": self.feed.kind == "simulated"
                    or (is_market_open() and can_enter_now(self.settings.no_new_entries_after)),
                    "clock": clock_label(),
                    "trading_day": self._day.isoformat(),
                    "halted_reason": self._halted_reason,
                    "last_error": self._last_error,
                },
                "account": {
                    "equity": round(equity, 2),
                    "cash": round(self.cash, 2),
                    "invested": round(invested, 2),
                    "holdings_value": round(self._holdings_value(), 2),
                    "day_pnl": round(day_pnl, 2),
                    "day_pnl_pct": round(day_pnl_pct, 2),
                    "unrealised": round(unrealised, 2),
                    "realised_today": round(self.store.realised_pnl_today(), 2),
                    "realised_total": round(self.store.realised_pnl_total(), 2),
                    "starting_equity": round(self._day_start_equity, 2),
                },
                "positions": positions,
                "holdings": [
                    {
                        "symbol": h["symbol"],
                        "qty": h["qty"],
                        "ltp": round(self.prices.get(h["symbol"], h.get("average_price", 0.0)), 2),
                    }
                    for h in self.holdings
                ],
                "signals": signals,
                "risk": {
                    "level": self.settings.risk_level,
                    "label": preset.label,
                    "max_open_positions": preset.max_open_positions,
                    "risk_per_trade_pct": preset.risk_per_trade_pct,
                    "max_position_pct": preset.max_position_pct,
                    "daily_loss_limit_pct": preset.daily_loss_limit_pct,
                    "daily_target_pct": preset.daily_target_pct,
                },
                "settings": self.settings.public_dict(),
                "scan": {
                    "scanning": self._scanning,
                    "last": self.store.get_json("last_scan", None),
                },
                "activity": self.store.events(18),
                "updated_at": now_ist().strftime("%H:%M:%S"),
            }

    # ----------------------------------------------------------- saved prefs
    def load_saved_prefs(self) -> None:
        risk = self.store.get_kv("risk_level")
        if risk in PRESETS:
            self.settings.risk_level = risk
        universe = self.store.get_json("universe", None)
        if isinstance(universe, list) and universe:
            self.settings.universe = [str(s).upper() for s in universe]
        # Restoring the mode is not enough: the executor was built for the mode
        # from .env, so it has to be rebuilt here or the UI would say "live"
        # while orders were still filled on paper.
        mode = self.store.get_kv("mode")
        if mode in {"paper", "live"} and mode != self.mode:
            if mode == "live" and not self.settings.has_credentials:
                self.store.add_event("warn", "Saved live mode ignored: no Groww credentials in .env")
                return
            executor, error = build_executor(self.settings, mode)
            if mode == "live" and executor.kind != "groww":
                self.store.add_event("warn", f"Saved live mode unavailable: {error}")
                return
            self.mode = mode
            self.executor = executor
            self.positions = {
                p["symbol"]: p for p in self.store.open_positions() if p.get("mode", mode) == mode
            }
            self.holdings = executor.holdings() if mode == "live" else []
            self._last_holdings_refresh = time.time()
            live_cash = executor.cash() if mode == "live" else None
            if live_cash is not None:
                self.cash = live_cash
                self._store_cash()
            elif mode == "paper":
                self.cash = float(self.store.get_kv("paper_cash", str(self.settings.capital)))
            self._day_start_equity = self._equity()
            self.store.add_event(
                "info",
                f"Restored {mode.upper()} mode | prices: {self.feed.kind} | orders: {self.executor.kind}",
            )
