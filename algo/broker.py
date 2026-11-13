"""Order execution: Groww live orders, or a paper executor.

Only order placement, order status and funds need the Groww API, and all of
those are included in Groww's free plan. Prices and candles are deliberately
not fetched here - see ``algo/datafeed.py``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .config import Settings

log = logging.getLogger("growwalgo.broker")


class BrokerError(RuntimeError):
    """Raised when the broker rejects a request; message is shown in the UI."""


@dataclass
class Fill:
    symbol: str
    side: str
    qty: int
    price: float
    order_id: str
    simulated: bool = False


def _num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class Executor:
    kind = "base"

    def place(self, side: str, symbol: str, qty: int, ref_price: float, settings: Settings) -> Fill:
        raise NotImplementedError

    def cash(self) -> float | None:
        return None

    def holdings(self) -> list[dict]:
        """Existing delivery holdings. The bot never trades these."""
        return []


class PaperExecutor(Executor):
    """Fills at the reference price plus a small slippage assumption."""

    kind = "paper"

    def place(self, side: str, symbol: str, qty: int, ref_price: float, settings: Settings) -> Fill:
        slip = ref_price * settings.slippage_pct * (1 if side.upper() == "BUY" else -1)
        return Fill(
            symbol,
            side.upper(),
            int(qty),
            round(ref_price + slip, 2),
            f"PAPER-{int(time.time() * 1000) % 10_000_000}",
            True,
        )


class GrowwExecutor(Executor):
    """Places real orders through the Groww Trading API (free plan is enough)."""

    kind = "groww"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = None

    # -- connection --------------------------------------------------------
    def _build_client(self):
        try:
            from growwapi import GrowwAPI
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise BrokerError("growwapi is not installed. Run: pip install growwapi") from exc

        token = self.settings.access_token
        if not token:
            try:
                if self.settings.api_key and self.settings.totp_secret:
                    import pyotp

                    token = GrowwAPI.get_access_token(
                        api_key=self.settings.api_key,
                        totp=pyotp.TOTP(self.settings.totp_secret).now(),
                    )
                elif self.settings.api_key and self.settings.api_secret:
                    token = GrowwAPI.get_access_token(
                        api_key=self.settings.api_key, secret=self.settings.api_secret
                    )
            except Exception as exc:  # noqa: BLE001 - surface any auth failure
                raise BrokerError(f"Groww authentication failed: {exc}") from exc

        if isinstance(token, dict):
            token = token.get("token") or token.get("access_token") or ""
        if not token:
            raise BrokerError(
                "No Groww credentials. Add GROWW_ACCESS_TOKEN (or GROWW_API_KEY + "
                "GROWW_API_SECRET / GROWW_TOTP_SECRET) to .env"
            )
        return GrowwAPI(str(token))

    @property
    def client(self):
        if self._client is None:
            self._client = self._build_client()
        return self._client

    def reset(self) -> None:
        self._client = None

    def _call(self, name: str, *args, **kwargs):
        method = getattr(self.client, name)
        try:
            return method(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - SDK exception classes vary by version
            text = str(exc)
            if "401" in text or "authent" in text.lower() or "expired" in text.lower():
                self.reset()
            raise BrokerError(text) from exc

    # -- trading -----------------------------------------------------------
    def place(self, side: str, symbol: str, qty: int, ref_price: float, settings: Settings) -> Fill:
        reference = f"GA{int(time.time())}{symbol[:3]}".upper().replace("_", "")[:20]
        price = round(ref_price, 2)
        is_buy = side.upper() == "BUY"
        order = self._call(
            "place_order",
            validity="DAY",
            exchange="NSE",
            # Entries use a limit at the last price for price control; exits use
            # a market order so a stop can always get out.
            order_type="LIMIT" if is_buy else "MARKET",
            product="CNC",
            quantity=int(qty),
            segment="CASH",
            trading_symbol=symbol,
            transaction_type="BUY" if is_buy else "SELL",
            price=price if is_buy else 0.0,
            order_reference_id=reference,
        )
        order_id = ""
        if isinstance(order, dict):
            order_id = str(order.get("groww_order_id") or order.get("order_id") or "")
        if not order_id:
            raise BrokerError(f"Order not accepted: {order}")

        fill_price, filled_qty = self._await_fill(order_id, side, symbol, qty)
        if filled_qty <= 0:
            raise BrokerError(f"{side} {symbol} filled 0 shares")
        return Fill(symbol, side.upper(), filled_qty, fill_price, order_id)

    def _await_fill(self, order_id: str, side: str, symbol: str, qty: int) -> tuple[float, int]:
        """Poll until the order reaches a final state.

        Returns (average price, filled quantity). A partial fill is reported
        honestly so the engine never assumes shares it does not own; an
        unfilled order is cancelled and raises instead of inventing a fill.
        """
        deadline = time.time() + 15
        last_status = ""
        filled = 0
        avg = 0.0
        while time.time() < deadline:
            try:
                detail = self._call("get_order_detail", segment="CASH", groww_order_id=order_id)
            except BrokerError:
                detail = {}
            if isinstance(detail, dict):
                status = str(detail.get("order_status", "")).upper()
                last_status = status or last_status
                filled = int(_num(detail.get("filled_quantity"), filled))
                avg = _num(detail.get("average_fill_price"), avg)
                if status in {"EXECUTED", "COMPLETED", "DELIVERY_AWAITED"}:
                    if filled > 0 and avg > 0:
                        return avg, filled
                    break
                if status in {"REJECTED", "FAILED", "CANCELLED"}:
                    raise BrokerError(
                        f"{side} {symbol} {status}: {detail.get('remark', 'rejected by broker')}"
                    )
                if filled >= qty and avg > 0:
                    return avg, filled
            time.sleep(1.0)

        try:
            self._call("cancel_order", segment="CASH", groww_order_id=order_id)
        except BrokerError:
            pass

        if filled > 0 and avg > 0:
            log.warning("Order %s partially filled (%s/%s) - reporting executed quantity", order_id, filled, qty)
            return avg, filled
        raise BrokerError(
            f"{side} {symbol} did not fill (last status: {last_status or 'unknown'}) - order cancelled"
        )

    def cash(self) -> float | None:
        try:
            payload = self._call("get_available_margin_details")
        except BrokerError:
            return None
        if not isinstance(payload, dict):
            return None
        # Groww reports spendable cash as "clear_cash"; other plans may use one
        # of the aliases below. Getting this wrong would over-size orders.
        for key in (
            "clear_cash",
            "available_cash",
            "available_margin",
            "net_margin_available",
            "cash",
            "available_balance",
        ):
            if key in payload:
                value = _num(payload[key], -1)
                if value >= 0:
                    return value
        for value in payload.values():
            if isinstance(value, dict):
                for key in ("clear_cash", "available_cash", "available_margin", "net_margin_available"):
                    if key in value:
                        return _num(value[key])
        return None

    def holdings(self) -> list[dict]:
        try:
            payload = self._call("get_holdings_for_user")
        except BrokerError:
            return []
        rows = payload.get("holdings", []) if isinstance(payload, dict) else []
        out: list[dict] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            qty = int(_num(row.get("quantity"), 0))
            symbol = str(row.get("trading_symbol") or "").upper()
            if qty <= 0 or not symbol:
                continue
            out.append(
                {"symbol": symbol, "qty": qty, "average_price": _num(row.get("average_price"), 0.0)}
            )
        return out


def build_executor(settings: Settings, mode: str) -> tuple[Executor, str | None]:
    """Return the executor for this mode plus an error to surface in the UI."""
    if mode != "live":
        return PaperExecutor(), None
    if not settings.has_credentials:
        return PaperExecutor(), "Live mode needs Groww API credentials in .env - staying in paper mode"
    try:
        executor = GrowwExecutor(settings)
        _ = executor.client  # force auth so failures appear immediately
        return executor, None
    except BrokerError as exc:
        return PaperExecutor(), f"Groww authentication failed ({exc}) - staying in paper mode"
