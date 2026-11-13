"""Market-data feeds.

Groww's free API plan includes order placement and portfolio endpoints but
excludes live quotes, LTP and historical candles. Since the whole point here is
to avoid a paid data subscription, prices and candles come from Yahoo Finance's
public chart endpoint: no key, no account, no cost.

Groww is still the broker in live mode; it just is not the price source. The
fallback simulator keeps the app usable when there is no network at all.
"""

from __future__ import annotations

import json
import logging
import math
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from .config import SIM_BASE_PRICES, Settings
from .market import IST, now_ist
from .strategy import Candle

log = logging.getLogger("growwalgo.feed")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

# Groww interval constants -> Yahoo interval tokens.
_INTERVALS = {
    "1minute": ("1m", "5d"),
    "2minute": ("2m", "5d"),
    "5minute": ("5m", "5d"),
    "10minute": ("15m", "1mo"),
    "15minute": ("15m", "1mo"),
    "30minute": ("30m", "1mo"),
    "1hour": ("60m", "3mo"),
    "4hour": ("60m", "3mo"),
    "1day": ("1d", "1y"),
}


class DataError(RuntimeError):
    """Raised when a feed cannot provide prices or candles."""


class Feed:
    """Interface used by the engine."""

    kind = "base"

    def ltp(self, symbols: list[str]) -> dict[str, float]:
        raise NotImplementedError

    def candles(self, symbol: str, settings: Settings) -> list[Candle]:
        raise NotImplementedError

    def daily_candles(self, symbol: str, bars: int = 160) -> list[Candle]:
        """Daily history, used by the market screener. Optional for feeds."""
        return []


# ---------------------------------------------------------------------------
# Yahoo Finance (free, keyless)
# ---------------------------------------------------------------------------


class YahooFeed(Feed):
    kind = "yahoo"
    BASE = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.suffix = settings.instrument_suffix or ".NS"
        self._cache: dict[str, tuple[float, list[Candle], float]] = {}
        self._daily_cache: dict[str, tuple[float, list[Candle]]] = {}
        self._lock = threading.Lock()
        self._failing: set[str] = set()

    # -- helpers -----------------------------------------------------------
    def ticker(self, symbol: str) -> str:
        if "." in symbol:
            return symbol
        return f"{symbol}{self.suffix}"

    def _params(self) -> str:
        interval, span = _INTERVALS.get(self.settings.candle_interval, ("5m", "5d"))
        return urllib.parse.urlencode(
            {"interval": interval, "range": span, "includePrePost": "false"}
        )

    def _download(self, ticker: str, params: str | None = None) -> dict:
        url = f"{self.BASE.format(ticker=urllib.parse.quote(ticker))}?{params or self._params()}"
        request = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                return json.loads(response.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            raise DataError(f"price feed unavailable: {exc}") from exc

    def _parse(self, payload: dict, symbol: str) -> tuple[list[Candle], float]:
        try:
            result = payload["chart"]["result"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise DataError(f"{symbol}: unexpected response from the price feed") from exc

        meta = result.get("meta") or {}
        stamps = result.get("timestamp") or []
        quote = (result.get("indicators") or {}).get("quote") or [{}]
        q = quote[0] or {}
        opens = q.get("open") or []
        highs = q.get("high") or []
        lows = q.get("low") or []
        closes = q.get("close") or []
        volumes = q.get("volume") or []

        candles: list[Candle] = []
        for i, stamp in enumerate(stamps):
            close = closes[i] if i < len(closes) else None
            if close is None:
                continue
            try:
                opened = opens[i] if i < len(opens) and opens[i] is not None else close
                high = highs[i] if i < len(highs) and highs[i] is not None else close
                low = lows[i] if i < len(lows) and lows[i] is not None else close
                volume = volumes[i] if i < len(volumes) and volumes[i] is not None else 0
                candles.append(
                    Candle(
                        ts=datetime.fromtimestamp(int(stamp), tz=timezone.utc).astimezone(IST),
                        open=float(opened),
                        high=float(high),
                        low=float(low),
                        close=float(close),
                        volume=float(volume),
                    )
                )
            except (TypeError, ValueError):
                continue

        if not candles:
            raise DataError(f"{symbol}: no candles returned")

        candles.sort(key=lambda c: c.ts)
        price = float(meta.get("regularMarketPrice") or candles[-1].close)
        return candles, price

    def _load(self, symbol: str, force: bool = False) -> tuple[list[Candle], float]:
        ttl = max(15, self.settings.candle_refresh_seconds)
        with self._lock:
            cached = self._cache.get(symbol)
        if cached and not force and time.time() - cached[0] < ttl:
            return cached[1], cached[2]

        try:
            candles, price = self._parse(self._download(self.ticker(symbol)), symbol)
        except DataError:
            if cached:
                # Serve the last good snapshot rather than dropping the symbol.
                return cached[1], cached[2]
            raise

        with self._lock:
            self._cache[symbol] = (time.time(), candles, price)
            self._failing.discard(symbol)
        return candles, price

    # -- interface ---------------------------------------------------------
    def ltp(self, symbols: list[str]) -> dict[str, float]:
        prices: dict[str, float] = {}
        for symbol in symbols:
            try:
                _, price = self._load(symbol)
            except DataError as exc:
                if symbol not in self._failing:
                    self._failing.add(symbol)
                    log.warning("%s", exc)
                continue
            if price > 0:
                prices[symbol] = round(price, 2)
        if not prices:
            raise DataError("no prices could be fetched (network down?)")
        return prices

    def candles(self, symbol: str, settings: Settings) -> list[Candle]:
        candles, _ = self._load(symbol)
        return candles

    def daily_candles(self, symbol: str, bars: int = 160) -> list[Candle]:
        """Daily bars for trend/volatility scoring, cached for six hours."""
        with self._lock:
            cached = self._daily_cache.get(symbol)
        if cached and time.time() - cached[0] < 6 * 3600:
            return cached[1]
        payload = self._download(
            self.ticker(symbol), "interval=1d&range=1y&includePrePost=false"
        )
        candles, _ = self._parse(payload, symbol)
        candles = candles[-bars:]
        with self._lock:
            self._daily_cache[symbol] = (time.time(), candles)
        return candles

    def probe(self, symbol: str) -> None:
        """Fail fast at startup if the feed cannot be reached."""
        self._load(symbol, force=True)


# ---------------------------------------------------------------------------
# Local simulator (offline demo)
# ---------------------------------------------------------------------------


class SimulatedFeed(Feed):
    """Random-walk prices so the dashboard works with no network at all."""

    kind = "simulated"

    def __init__(self, settings: Settings, seed: int | None = None) -> None:
        self.settings = settings
        self._rng = random.Random(seed)
        self._anchors: dict[str, float] = dict(SIM_BASE_PRICES)
        self._last_tick = time.time()

    def _base(self, symbol: str) -> float:
        if symbol not in self._anchors:
            self._anchors[symbol] = 50 + (abs(hash(symbol)) % 9000) / 100.0
        return self._anchors[symbol]

    def _advance(self) -> None:
        elapsed = time.time() - self._last_tick
        if elapsed < 1.0:
            return
        self._last_tick = time.time()
        steps = min(30, max(1, int(elapsed // 2)))
        for symbol in list(self._anchors):
            for _ in range(steps):
                drift = math.sin(time.time() / 240 + (abs(hash(symbol)) % 100) / 10) * 0.00025
                shock = self._rng.gauss(0, 0.0003)
                self._anchors[symbol] = max(1.0, self._anchors[symbol] * (1 + drift + shock))

    def _series(self, symbol: str) -> list[Candle]:
        self._advance()
        minutes = self.settings.candle_minutes
        bars = max(self.settings.lookback_bars, 90)
        live = self._base(symbol)
        end = now_ist().replace(second=0, microsecond=0)
        rng = random.Random(f"{symbol}-{now_ist().date()}")
        phase = (abs(hash(symbol)) % 100) / 8.0

        closes: list[float] = []
        price = live * (1 + rng.uniform(-0.015, 0.015))
        for i in range(bars):
            cycle = math.sin(i / 16.0 + phase) * 0.0013
            price = max(1.0, price * (1 + cycle + rng.gauss(0, 0.0030)))
            closes.append(price)

        scale = live / closes[-1]
        candles: list[Candle] = []
        for i, raw_close in enumerate(closes):
            close = raw_close * scale
            open_ = closes[i - 1] * scale if i else close * (1 - rng.gauss(0, 0.0012))
            wick = abs(rng.gauss(0, 0.0022)) + 0.0004
            candles.append(
                Candle(
                    ts=end - timedelta(minutes=minutes * (bars - 1 - i)),
                    open=round(open_, 2),
                    high=round(max(open_, close) * (1 + wick), 2),
                    low=round(min(open_, close) * (1 - wick), 2),
                    close=round(close, 2),
                    volume=float(rng.randint(20_000, 500_000)),
                )
            )
        return candles

    def ltp(self, symbols: list[str]) -> dict[str, float]:
        self._advance()
        return {symbol: round(self._base(symbol), 2) for symbol in symbols}

    def candles(self, symbol: str, settings: Settings) -> list[Candle]:
        return self._series(symbol)


def build_feed(settings: Settings) -> Feed:
    """Yahoo by default; simulator only if the feed is unreachable or disabled."""
    if settings.data_source == "simulated":
        return SimulatedFeed(settings)

    feed = YahooFeed(settings)
    probe_symbol = settings.universe[0] if settings.universe else "TATASTEEL"
    try:
        feed.probe(probe_symbol)
        log.info("Free price feed ready (Yahoo Finance, no subscription)")
        return feed
    except DataError as exc:
        log.warning("Free price feed unavailable (%s) - using the simulator", exc)
        return SimulatedFeed(settings)
