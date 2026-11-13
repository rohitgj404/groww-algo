# GrowwAlgo

An automated, medium-risk NSE trading engine for a small account (₹2,500 default),
built for **Groww's free API plan**, with a live dashboard for monitoring and control.

- **Prices:** free, keyless Yahoo Finance feed — no paid market-data subscription
- **Orders:** Groww Trading API (free plan includes order placement and portfolio)
- **Risk:** ATR stops, position caps, daily loss limit, per-symbol cooldown
- **Dashboard:** FastAPI + WebSocket, Midnight Black / Electric Blue / Cantaloupe Orange, tables only

---

## Why this works on Groww's free plan

Groww's ₹0 plan includes authentication, order placement/modification/cancellation,
positions, holdings and margin. It excludes **live quotes, LTP, OHLC and historical
candles** — exactly the parts a strategy needs. So the bot splits the job:

| Job | Source | Cost |
| --- | --- | --- |
| Hourly candles + last price | Yahoo Finance public chart endpoint | ₹0 |
| Buy / sell orders, funds | Groww Trading API | ₹0 (plan) |

You still pay normal statutory charges per trade: brokerage (lower of ₹20 and 0.05% per
leg), STT, exchange/SEBI fees, GST, stamp duty, and the **₹16.5 DP charge on every
delivery sell**. On a ₹2,500 account that friction is significant, which is why the bot
has a 30-minute cooldown and takes selective setups instead of scalping all day.

---

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -r requirements.txt

copy .env.example .env            # optional: add Groww keys for live mode
python run.py --open
```

Dashboard: <http://127.0.0.1:8080>. Press **Start engine**, or set `AUTO_START=true`
in `.env` for a fully unattended launch.

Without any Groww credentials the bot runs in **paper mode on real market prices** —
useful for judging the strategy before risking money. If the price feed is unreachable
it falls back to a local simulator, clearly labelled `SIMULATED FEED`.

### Groww credentials (free plan)

1. In Groww: Profile → Settings → Trading APIs, choose the **₹0 Free Trial** plan.
2. Create a key and put **one** of these in `.env`:

| Method | Variables | Notes |
| --- | --- | --- |
| Access token | `GROWW_ACCESS_TOKEN` | Expires daily at 06:00 IST |
| API key + secret | `GROWW_API_KEY`, `GROWW_API_SECRET` | Token generated on startup |
| TOTP (no expiry) | `GROWW_API_KEY`, `GROWW_TOTP_SECRET` | Requires `pyotp` |

Live mode refuses to start without credentials, and the first switch asks for confirmation.

---

## Market-wide screener

You do not pick the stocks — the bot scans the whole market and keeps the best few.

1. **NSE daily file** (`sec_bhavdata_full`): every listed equity with close, volume,
turnover and delivery % in one request (~2,700 stocks).
2. **Affordability + liquidity filter**: one share must fit free cash and the position
   cap; turnover ≥ ₹2 crore; delivery ≥ 10%.
3. **Trend scoring** on Yahoo daily candles for the shortlist: EMA20 above EMA50, price
   above EMA20, trend not parabolic (<15%), ATR between 0.8% and 9%, 20-day volume above
   100k. The top 20 by score become the watchlist.

It runs at startup, every 12 hours, or when you press **Rescan market**. A scan takes
about 10 seconds. If no stock qualifies, the current watchlist is kept and the dashboard
says so. Tune with `SCAN_SIZE`, `MIN_TURNOVER_LACS`, `MIN_PRICE`, `AUTO_SCREEN`.

## The strategy

Long-only cash equity (`CNC` delivery), scanned every 20 seconds on **closed 1-hour candles**.
The timeframe is hourly because a 5-minute ATR target (~0.5%) is smaller than the ~1.4%
round-trip cost on a ₹1,500 delivery trade; hourly targets average ~3% and can pay for
themselves. Change `CANDLE_INTERVAL` / `CANDLE_MINUTES` to alter it.
A trade is taken only when all of these align on the last *closed* candle:

1. `EMA(9) > EMA(21)` — short-term trend is up
2. `close > EMA(21)` — price confirms the trend
3. `RSI(14)` between **50 and 72** — momentum, not overextended
4. `close > previous candle high` — a fresh push, not a falling knife
5. `ATR / price < 6%` — skip violently erratic names

**Exit** on whichever comes first: ATR stop, ATR target, trailing stop (ratchets up after
1 ATR of profit), EMA cross down, or RSI ≥ 78.

A setup is also rejected when its target is smaller than the cost of a delivery
round-trip (`MIN_EDGE_PCT`, default 2.5%). At this account size the ₹16.5 DP fee plus
taxes is about 1.4% of a ₹1,000 position, so a 0.5% target would lose money even when it
works. This filter is why the bot trades rarely — and why intraday (`MIS`) execution,
which avoids the DP fee entirely, is the natural next step for this account size. After any exit the symbol is skipped for
30 minutes so the bot cannot immediately re-buy the level it was just stopped out of.

### Risk rules (Balanced / medium)

| Control | Value |
| --- | --- |
| Risk per trade | 1.0% of equity (~₹25 on ₹2,500) |
| Max position size | 40% of equity (~₹1,000) |
| Max concurrent positions | 2 |
| Stop distance | 1.6 × ATR(14) |
| Target distance | 2.4 × ATR(14) → 1.5 : 1 reward/risk |
| Daily loss limit | −2% → no new entries until tomorrow |
| Daily target | +2.5% → profit protected, no new entries |
| New entries cut-off | 15:00 IST |
| Cooldown after an exit | 30 minutes on that symbol |

Position size is the *smaller* of risk budget ÷ stop distance and the notional cap, so a
single stopped-out trade costs about ₹25. **Low** (0.5% risk, 1 position) and **High**
(1.5% risk, 3 positions) profiles are one click away.

---

## Dashboard

No charts — the numbers and tables you need, without duplicating the Groww app.

| Panel | What it shows |
| --- | --- |
| Header | Engine state, market phase, IST clock, equity, day P&L, free cash, invested |
| Open positions | Session start, realised/unrealised P&L, risk used; per-position stop and target |
| Controls | Start/pause, paper↔live, risk profile, watchlist, capital, data & order sources |
| Live signals | Every watchlist symbol with its signal reason and confidence |
| Trade log | Fills with reason and net P&L after charges |
| Activity | Engine decisions, trailing-stop moves, broker errors |

`Square off` closes everything at market. `Pause` stops new entries but keeps managing
existing stops.

### REST / WebSocket

```
GET  /api/state          full snapshot
GET  /api/trades         recent fills
POST /api/control        {"action": "start|pause|resume|stop|close_all|reset_paper|mode_paper|mode_live"}
POST /api/settings       {"risk_level": "...", "universe": [...], "capital": 2500}
WS   /ws                 snapshot pushed every second
```

---

## Project layout

```
run.py                 entry point (uvicorn + engine)
algo/config.py         settings, risk presets, watchlist defaults
algo/datafeed.py       free Yahoo price feed + offline simulator
algo/broker.py         Groww order executor + paper executor
algo/indicators.py     EMA / RSI / ATR (pure Python)
algo/strategy.py       signal + exit logic
algo/risk.py           position sizing, charges, portfolio guards
algo/engine.py         the trading loop and account state
algo/storage.py        SQLite: trades, positions, events
algo/server.py         FastAPI app
web/                   dashboard (HTML/CSS/JS, no CDN dependencies)
data/state.db          persisted state (created at runtime)
```

Entries are `CNC` limit orders at the last price; exits are market orders so a stop can
always get out. Unfilled entries are cancelled rather than assumed filled, and partial
fills are recorded at the quantity actually executed.

---

## Honest expectations

Markets cannot provide **guaranteed or constant earnings**. This bot reduces risk with
hard stops, position caps and daily loss limits, but it will have losing days — a 2%
daily stop on ₹2,500 is about ₹50, and the ₹16.5 DP fee on each delivery sell means a
₹900 round-trip trade needs roughly 2% just to break even. Run it in paper mode first,
review the realised P&L over several weeks, and only then consider live mode with money
you can afford to lose.

This project is for personal use and is not investment advice.
