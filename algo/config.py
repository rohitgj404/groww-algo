"""Central configuration for the GrowwAlgo trading system.

Values come from, in order of precedence:
  1. The dashboard (persisted in data/state.db, applied at runtime)
  2. Environment variables / .env file
  3. The defaults below

The default mode is PAPER. Live trading must be switched on deliberately.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
LOG_DIR = ROOT_DIR / "logs"
WEB_DIR = ROOT_DIR / "web"

load_dotenv(ROOT_DIR / ".env")

DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


# Liquid NSE cash-market names that a small account can actually size into.
# Symbols that Groww rejects are skipped and surfaced in the activity feed.
DEFAULT_UNIVERSE = [
    "TATASTEEL",
    "NHPC",
    "IRFC",
    "BEL",
    "IDEA",
    "SUZLON",
    "YESBANK",
    "SAIL",
]

# Roughly recent prices, used only by the offline simulator so the dashboard
# is fully demoable before API keys are added.
SIM_BASE_PRICES = {
    "TATASTEEL": 162.40,
    "NHPC": 84.60,
    "IRFC": 128.75,
    "BEL": 418.30,
    "IDEA": 13.85,
    "SUZLON": 61.20,
    "YESBANK": 21.65,
    "SAIL": 124.10,
    "PNB": 108.35,
    "JIOFIN": 318.00,
}


@dataclass(frozen=True)
class RiskPreset:
    label: str
    risk_per_trade_pct: float      # equity risked between entry and stop
    max_open_positions: int
    max_position_pct: float        # cap on notional per position
    atr_stop_mult: float           # stop distance in ATR units
    atr_target_mult: float         # take-profit distance in ATR units
    trail_trigger_atr: float       # profit (in ATR) before the stop starts trailing
    trail_distance_atr: float      # how far behind price the trailing stop sits
    daily_loss_limit_pct: float    # stop opening new trades after this loss
    daily_target_pct: float        # stop opening new trades after this gain


PRESETS: dict[str, RiskPreset] = {
    "conservative": RiskPreset(
        label="Conservative",
        risk_per_trade_pct=0.5,
        max_open_positions=1,
        max_position_pct=0.35,
        atr_stop_mult=2.0,
        atr_target_mult=3.0,
        trail_trigger_atr=1.5,
        trail_distance_atr=1.6,
        daily_loss_limit_pct=1.0,
        daily_target_pct=1.5,
    ),
    "balanced": RiskPreset(
        label="Balanced (medium risk)",
        risk_per_trade_pct=1.0,
        max_open_positions=2,
        max_position_pct=0.40,
        atr_stop_mult=1.6,
        atr_target_mult=2.4,
        trail_trigger_atr=1.0,
        trail_distance_atr=1.2,
        daily_loss_limit_pct=2.0,
        daily_target_pct=2.5,
    ),
    "aggressive": RiskPreset(
        label="Aggressive",
        risk_per_trade_pct=1.5,
        max_open_positions=3,
        max_position_pct=0.45,
        atr_stop_mult=1.4,
        atr_target_mult=2.1,
        trail_trigger_atr=0.8,
        trail_distance_atr=1.0,
        daily_loss_limit_pct=3.0,
        daily_target_pct=4.0,
    ),
}


@dataclass
class Settings:
    # --- account / broker -------------------------------------------------
    mode: str = os.getenv("TRADING_MODE", "paper").strip().lower()  # paper | live
    capital: float = _env_float("STARTING_CAPITAL", 2500.0)
    api_key: str = os.getenv("GROWW_API_KEY", "").strip()
    api_secret: str = os.getenv("GROWW_API_SECRET", "").strip()
    totp_secret: str = os.getenv("GROWW_TOTP_SECRET", "").strip()
    access_token: str = os.getenv("GROWW_ACCESS_TOKEN", "").strip()

    # --- data -------------------------------------------------------------
    # Groww's free plan excludes quotes and candles, so prices come from a
    # keyless public feed. Set to "simulated" to force the offline demo feed.
    data_source: str = os.getenv("DATA_SOURCE", "yahoo").strip().lower()
    instrument_suffix: str = os.getenv("INSTRUMENT_SUFFIX", ".NS").strip()

    # --- strategy ---------------------------------------------------------
    risk_level: str = os.getenv("RISK_LEVEL", "balanced").strip().lower()
    universe: list[str] = field(default_factory=lambda: list(DEFAULT_UNIVERSE))
    # Hourly bars by default: on a ₹1,500-per-trade delivery account a 5-minute
    # ATR target (~0.5%) is smaller than the round-trip cost, while an hourly
    # target (~2-3%) can actually pay for itself.
    candle_interval: str = os.getenv("CANDLE_INTERVAL", "1hour")
    candle_minutes: int = _env_int("CANDLE_MINUTES", 60)
    lookback_bars: int = _env_int("LOOKBACK_BARS", 120)
    ema_fast: int = 9
    ema_slow: int = 21
    rsi_period: int = 14
    atr_period: int = 14
    rsi_entry_low: float = 50.0
    rsi_entry_high: float = 72.0
    rsi_exit: float = 78.0
    min_candles: int = 40
    # A delivery round-trip costs roughly ₹16.5 (DP) plus ~0.3% in taxes and
    # brokerage. A target smaller than this cannot pay for itself, so such
    # setups are skipped instead of being taken as noise trades.
    min_edge_pct: float = _env_float("MIN_EDGE_PCT", 2.5)

    # --- execution --------------------------------------------------------
    poll_seconds: int = _env_int("POLL_SECONDS", 20)
    candle_refresh_seconds: int = _env_int("CANDLE_REFRESH_SECONDS", 60)
    slippage_pct: float = _env_float("SLIPPAGE_PCT", 0.0005)
    max_price_per_share: float = _env_float("MAX_PRICE_PER_SHARE", 800.0)
    auto_start: bool = os.getenv("AUTO_START", "false").strip().lower() in {"1", "true", "yes"}
    no_new_entries_after: str = os.getenv("NO_NEW_ENTRIES_AFTER", "15:00")

    # --- market screener ---------------------------------------------------
    # Scans the whole NSE equity catalogue and keeps the best few in the
    # watchlist, instead of trading a hand-picked list forever.
    auto_screen: bool = os.getenv("AUTO_SCREEN", "true").strip().lower() in {"1", "true", "yes"}
    scan_size: int = _env_int("SCAN_SIZE", 20)
    scan_candidates: int = _env_int("SCAN_CANDIDATES", 80)
    scan_refresh_hours: float = _env_float("SCAN_REFRESH_HOURS", 12.0)
    min_turnover_lacs: float = _env_float("MIN_TURNOVER_LACS", 200.0)
    min_price: float = _env_float("MIN_PRICE", 20.0)
    min_delivery_pct: float = _env_float("MIN_DELIVERY_PCT", 10.0)
    # After a position closes, ignore that symbol for a while so the bot does
    # not immediately re-buy the level it was just stopped out of.
    cooldown_minutes: int = _env_int("COOLDOWN_MINUTES", 30)

    # --- dashboard --------------------------------------------------------
    host: str = os.getenv("HOST", "127.0.0.1")
    port: int = _env_int("PORT", 8080)

    @property
    def preset(self) -> RiskPreset:
        return PRESETS.get(self.risk_level, PRESETS["balanced"])

    @property
    def has_credentials(self) -> bool:
        return bool(self.access_token or self.api_key or self.totp_secret)
    @property
    def fully_automatic(self) -> bool:
        return self.auto_start

    def public_dict(self) -> dict:
        """Settings safe to send to the browser (never tokens or secrets)."""
        data = asdict(self)
        for secret in ("api_key", "api_secret", "totp_secret", "access_token"):
            data.pop(secret, None)
        data["preset"] = asdict(self.preset)
        data["has_credentials"] = self.has_credentials
        return data
