"""Market-wide stock screener.

The engine should not trade a fixed 8-name list. This module scans the whole
NSE equity market and picks the handful of stocks worth watching today.

Data path, all free:
  1. NSE's daily "sec_bhavdata_full" file: every listed stock with close,
     volume, turnover and delivery percentage in a single request.
  2. Yahoo daily candles for the shortlist only, to score trend and volatility.

Selection is deliberately liquidity- and trend-aware: a long-only strategy
should watch stocks that are actually trending up, liquid enough to fill, and
priced so one share fits inside the account's position cap.
"""

from __future__ import annotations

import csv
import io
import logging
import statistics
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path

from .config import DATA_DIR, Settings
from .datafeed import Feed, YahooFeed
from .indicators import atr, ema
from .market import now_ist
from .storage import Store

log = logging.getLogger("growwalgo.screener")

BHAVCOPY_URL = "https://archives.nseindia.com/products/content/sec_bhavdata_full_{stamp}.csv"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"


@dataclass
class Candidate:
    symbol: str
    price: float
    turnover_lacs: float
    delivery_pct: float
    trend_pct: float          # EMA20 vs EMA50, in percent
    atr_pct: float            # ATR14 as a % of price
    score: float

    def to_dict(self) -> dict:
        return asdict(self)


def _get(url: str, timeout: int = 25) -> bytes | None:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/csv,application/json,*/*",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return None


def _parse_bhavcopy(raw: bytes) -> list[dict]:
    text = raw.decode("utf-8", "replace")
    reader = csv.reader(io.StringIO(text))
    try:
        header = [cell.strip() for cell in next(reader)]
    except StopIteration:
        return []

    index = {name: i for i, name in enumerate(header)}
    needed = ("SYMBOL", "SERIES", "CLOSE_PRICE", "TTL_TRD_QNTY", "TURNOVER_LACS", "DELIV_PER")
    if not all(key in index for key in needed):
        return []

    rows: list[dict] = []
    for cells in reader:
        if len(cells) < len(header):
            continue
        try:
            symbol = cells[index["SYMBOL"]].strip().upper()
            series = cells[index["SERIES"]].strip().upper()
            close = float(cells[index["CLOSE_PRICE"]].strip())
            volume = float(cells[index["TTL_TRD_QNTY"]].strip() or 0)
            turnover = float(cells[index["TURNOVER_LACS"]].strip() or 0)
            delivery = float(cells[index["DELIV_PER"]].strip() or 0)
        except (ValueError, IndexError):
            continue
        if series != "EQ" or close <= 0 or volume <= 0 or not symbol.replace("-", "").isalnum():
            continue
        rows.append(
            {
                "symbol": symbol,
                "close": close,
                "volume": volume,
                "turnover_lacs": turnover,
                "delivery_pct": delivery,
            }
        )
    return rows


class MarketScreener:
    """Finds the best tradeable names across the whole NSE equity market."""

    def __init__(self, settings: Settings, feed: Feed, store: Store) -> None:
        self.settings = settings
        self.feed = feed
        self.store = store
        self._bhav_cache: tuple[str, list[dict], float] | None = None

    # -- NSE daily snapshot ------------------------------------------------
    def _latest_bhavcopy(self, lookback_days: int = 8) -> tuple[str, list[dict]] | None:
        if self._bhav_cache and time.time() - self._bhav_cache[2] < 3600:
            return self._bhav_cache[0], self._bhav_cache[1]

        today = now_ist().date()
        for offset in range(lookback_days):
            day = today - timedelta(days=offset)
            if day.weekday() >= 5:  # NSE does not publish on weekends
                continue
            stamp = day.strftime("%d%m%Y")
            raw = _get(BHAVCOPY_URL.format(stamp=stamp))
            if not raw or raw[:9].lower().startswith(b"<!doctype"):
                continue
            rows = _parse_bhavcopy(raw)
            if len(rows) < 500:  # a real file lists ~2,000 equities
                continue
            self._bhav_cache = (day.isoformat(), rows, time.time())
            log.info("NSE daily snapshot %s: %s equities", day, len(rows))
            return day.isoformat(), rows
        return None

    # -- scoring -----------------------------------------------------------
    def _score(self, row: dict) -> Candidate | None:
        symbol = row["symbol"]
        try:
            candles = self.feed.daily_candles(symbol)
        except Exception as exc:  # noqa: BLE001 - one bad symbol must not stop the scan
            log.debug("%s: daily candles failed (%s)", symbol, exc)
            return None
        if len(candles) < 60:
            return None

        closes = [c.close for c in candles]
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        volumes = [c.volume for c in candles]

        e20 = ema(closes, 20)[-1]
        e50 = ema(closes, 50)[-1]
        atr14 = atr(highs, lows, closes, 14)[-1]
        if not all(isinstance(v, (int, float)) and v for v in (e20, e50, atr14)):
            return None

        price = closes[-1]
        trend_pct = (e20 / e50 - 1) * 100
        atr_pct = atr14 / price * 100
        avg_volume = statistics.mean(volumes[-20:]) if volumes else 0

        # Long-only: only consider names already in a daily uptrend.
        if e20 <= e50:
            return None
        if price < e20 or trend_pct > 15:  # trending, but not parabolic
            return None
        if not (0.8 <= atr_pct <= 9.0):
            return None
        if avg_volume < 100_000:
            return None

        liquidity = min(1.0, row["turnover_lacs"] / 2_000)  # ₹20 crore saturates
        vol_fit = max(0.0, 1 - abs(atr_pct - 2.5) / 6)
        score = (
            45 * min(1.0, trend_pct / 10)
            + 25 * liquidity
            + 20 * vol_fit
            + 10 * (1 if price > e20 else 0)
        )
        return Candidate(
            symbol=symbol,
            price=round(price, 2),
            turnover_lacs=round(row["turnover_lacs"], 1),
            delivery_pct=round(row["delivery_pct"], 1),
            trend_pct=round(trend_pct, 2),
            atr_pct=round(atr_pct, 2),
            score=round(score, 1),
        )

    # -- public ------------------------------------------------------------
    def run(self, equity: float, cash: float, size: int | None = None) -> dict:
        started = time.time()
        size = size or self.settings.scan_size

        if not isinstance(self.feed, YahooFeed):
            return {"ok": False, "message": "Screener needs the online price feed (Yahoo)"}

        snapshot = self._latest_bhavcopy()
        if not snapshot:
            return {"ok": False, "message": "NSE daily file unavailable - keeping the current watchlist"}
        day, rows = snapshot

        # One share must fit inside both free cash and the position cap.
        max_price = min(
            self.settings.max_price_per_share,
            equity * self.settings.preset.max_position_pct,
            cash if cash > 0 else self.settings.capital,
        )

        tradeable = [
            row
            for row in rows
            if self.settings.min_price <= row["close"] <= max_price
            and row["turnover_lacs"] >= self.settings.min_turnover_lacs
            and row["delivery_pct"] >= self.settings.min_delivery_pct
        ]
        if not tradeable:
            return {
                "ok": False,
                "message": f"No stock fits ₹{cash:,.0f} cash and the ₹{max_price:,.0f} position cap",
                "scanned": len(rows),
            }

        tradeable.sort(key=lambda row: row["turnover_lacs"], reverse=True)
        shortlist = tradeable[: max(10, self.settings.scan_candidates)]

        scored: list[Candidate] = []
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(self._score, row) for row in shortlist]
            for future in as_completed(futures):
                candidate = future.result()
                if candidate:
                    scored.append(candidate)

        scored.sort(key=lambda c: c.score, reverse=True)
        selected = scored[:size]

        summary = {
            "ok": bool(selected),
            "day": day,
            "scanned": len(rows),
            "tradeable": len(tradeable),
            "scored": len(scored),
            "selected": [c.symbol for c in selected],
            "picks": [c.to_dict() for c in selected],
            "max_price": round(max_price, 2),
            "seconds": round(time.time() - started, 1),
            "at": now_ist().strftime("%Y-%m-%d %H:%M:%S"),
            "epoch": time.time(),
            "message": (
                f"Screened {len(rows):,} NSE stocks -> {len(tradeable):,} affordable and liquid "
                f"-> {len(selected)} in an uptrend selected"
                if selected
                else f"Screened {len(rows):,} NSE stocks but none are in a confirmed uptrend right now"
            ),
        }
        self.store.set_json("last_scan", summary)
        log.info(summary["message"])
        return summary
