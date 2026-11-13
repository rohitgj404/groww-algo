"""Indian market clock helpers (IST), with holiday-free weekday logic."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

OPEN_TIME = time(9, 15)
CLOSE_TIME = time(15, 30)
PRE_OPEN_TIME = time(9, 0)


def now_ist() -> datetime:
    return datetime.now(IST)


def is_weekday(day: date) -> bool:
    return day.weekday() < 5


def is_market_open(moment: datetime | None = None) -> bool:
    moment = (moment or now_ist()).astimezone(IST)
    if not is_weekday(moment.date()):
        return False
    return OPEN_TIME <= moment.time() <= CLOSE_TIME


def session_date(moment: datetime | None = None) -> date:
    return (moment or now_ist()).astimezone(IST).date()


def clock_label(moment: datetime | None = None) -> str:
    return (moment or now_ist()).astimezone(IST).strftime("%H:%M:%S IST")


def market_phase(moment: datetime | None = None) -> str:
    """Human-readable phase used by the dashboard badge."""
    moment = (moment or now_ist()).astimezone(IST)
    if not is_weekday(moment.date()):
        return "Weekend"
    t = moment.time()
    if t < PRE_OPEN_TIME:
        return "Pre-market"
    if t < OPEN_TIME:
        return "Pre-open"
    if t <= CLOSE_TIME:
        return "Market open"
    return "Closed"


def next_open(moment: datetime | None = None) -> datetime:
    moment = (moment or now_ist()).astimezone(IST)
    candidate = moment.replace(hour=OPEN_TIME.hour, minute=OPEN_TIME.minute, second=0, microsecond=0)
    if candidate <= moment:
        candidate += timedelta(days=1)
    while not is_weekday(candidate.date()):
        candidate += timedelta(days=1)
    return candidate


def can_enter_now(no_entry_after: str = "15:00") -> bool:
    """Entries stop before the close so positions are not opened into the bell."""
    moment = now_ist()
    if not is_market_open(moment):
        return False
    try:
        hour, minute = (int(part) for part in no_entry_after.split(":"))
    except (ValueError, AttributeError):
        hour, minute = 15, 0
    return moment.time() < time(hour, minute)
