/* =========================================================================
   GrowwAlgo dashboard client
   Live state over WebSocket, REST for controls. Numbers and tables only.
   ========================================================================= */

const $ = (id) => document.getElementById(id);

const ui = {
  state: null,
  ws: null,
  retries: 0,
  busy: false,
  trades: [],
};

const money = (value, withSign = false) => {
  const n = Number(value || 0);
  const sign = withSign ? (n > 0 ? "+" : n < 0 ? "−" : "") : n < 0 ? "−" : "";
  return `${sign}₹${Math.abs(n).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
};

const pct = (value) => {
  const n = Number(value || 0);
  return `${n > 0 ? "+" : ""}${n.toFixed(2)}%`;
};

const cls = (value) => (Number(value) > 0 ? "up" : Number(value) < 0 ? "down" : "neutral");

function toast(message, kind = "ok") {
  const stack = $("toasts");
  const node = document.createElement("div");
  node.className = `toast ${kind}`;
  node.textContent = message;
  stack.appendChild(node);
  setTimeout(() => {
    node.style.opacity = "0";
    node.style.transform = "translateY(6px)";
    node.style.transition = "all .25s ease";
    setTimeout(() => node.remove(), 260);
  }, 3800);
}

/* ---------------------------------------------------------------- network */
async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  return response.json();
}

async function control(action) {
  if (ui.busy) return;
  ui.busy = true;
  try {
    const data = await api("/api/control", {
      method: "POST",
      body: JSON.stringify({ action }),
    });
    if (data.result) toast(data.result.message, data.result.ok ? "ok" : "err");
    if (data.status) render(data);
  } catch (error) {
    toast(`Request failed: ${error.message}`, "err");
  } finally {
    ui.busy = false;
  }
}

async function saveSettings(patch) {
  try {
    const data = await api("/api/settings", {
      method: "POST",
      body: JSON.stringify(patch),
    });
    if (data.result) toast(data.result.message, data.result.ok ? "ok" : "err");
    if (data.status) render(data);
  } catch (error) {
    toast(`Could not save: ${error.message}`, "err");
  }
}

async function loadTrades() {
  try {
    const data = await api("/api/trades?limit=40");
    ui.trades = data.trades || [];
    renderTrades();
  } catch {
    /* the socket keeps retrying */
  }
}

function connect() {
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${protocol}://${location.host}/ws`);

  socket.onopen = () => {
    ui.retries = 0;
  };

  socket.onmessage = (event) => {
    try {
      render(JSON.parse(event.data));
    } catch {
      /* ignore malformed frames */
    }
  };

  socket.onclose = () => {
    $("stateText").textContent = "Reconnecting";
    $("statePill").className = "pill paused";
    ui.retries += 1;
    setTimeout(connect, Math.min(8000, 700 * ui.retries));
  };

  socket.onerror = () => socket.close();
  ui.ws = socket;
}

/* ----------------------------------------------------------------- render */
function render(data) {
  if (!data || !data.status) return;
  ui.state = data;

  const { status, account, risk, settings, positions, signals, activity } = data;
  const holdings = data.holdings || [];
  const scan = data.scan || {};

  const running = status.state === "running";
  const paused = status.state === "paused";
  $("statePill").className = `pill ${running ? "running" : paused ? "paused" : ""}`;
  $("stateText").textContent = running ? "Running" : paused ? "Paused" : "Stopped";
  $("marketPill").className = `pill ghost ${status.market_open ? "running" : ""}`;
  $("marketText").textContent = status.market;
  $("clock").textContent = status.clock;

  $("equityValue").textContent = money(account.equity);
  $("dayPnl").textContent = `${money(account.day_pnl, true)}  ${pct(account.day_pnl_pct)}`;
  $("dayPnl").className = `mono ${cls(account.day_pnl)}`;
  $("cashValue").textContent = money(account.cash);
  $("investedValue").textContent = money(account.invested);

  const runBtn = $("btnRun");
  runBtn.className = `btn ${running ? "ghost" : "primary"}`;
  $("btnRunText").textContent = running ? "Pause engine" : paused ? "Resume engine" : "Start engine";
  $("btnFlatten").disabled = positions.length === 0;

  $("modeBadge").textContent = status.mode.toUpperCase();
  $("modeBadge").className = `pill small ${status.mode === "live" ? "live" : ""}`;
  $("feedBadge").textContent =
    status.feed === "simulated" ? "SIMULATED FEED" : status.feed === "yahoo" ? "FREE DATA · YAHOO" : status.feed.toUpperCase();
  $("feedLabel").textContent =
    status.feed === "yahoo" ? "Yahoo Finance (free)" : status.feed === "simulated" ? "Offline simulator" : status.feed;
  $("execLabel").textContent =
    status.execution === "groww" ? "Groww API (live money)" : "Paper fills (no money)";

  $("statStart").textContent = money(account.starting_equity);
  $("statRealised").textContent = money(account.realised_today, true);
  $("statRealised").className = `mono ${cls(account.realised_today)}`;
  $("statUnrealised").textContent = money(account.unrealised, true);
  $("statUnrealised").className = `mono ${cls(account.unrealised)}`;
  $("statRisk").textContent = `${positions.length} / ${risk.max_open_positions}`;

  $("holdingsStat").hidden = holdings.length === 0;
  $("statHoldings").textContent = money(account.holdings_value || 0);
  $("holdingsNote").hidden = holdings.length === 0;
  if (holdings.length) {
    $("holdingsNote").textContent =
      `Existing Groww holdings, included in equity but never traded by the bot: ` +
      holdings.map((h) => `${h.symbol} × ${h.qty}`).join(", ");
  }

  document.querySelectorAll("#modeGroup .seg").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.mode === status.mode);
  });
  document.querySelectorAll("#riskGroup .seg").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.risk === risk.level);
  });
  $("modeHint").textContent =
    status.mode === "live"
      ? "LIVE: orders are sent to your Groww account with real money."
      : status.feed === "simulated"
      ? "Simulated prices — no network feed available."
      : "Paper fills at live prices from the free feed.";

  $("riskLimits").innerHTML = `
    <li><span>Risk per trade</span><span>${risk.risk_per_trade_pct.toFixed(1)}% of equity</span></li>
    <li><span>Max position</span><span>${(risk.max_position_pct * 100).toFixed(0)}% of equity</span></li>
    <li><span>Max open</span><span>${risk.max_open_positions} position(s)</span></li>
    <li><span>Daily stop</span><span>−${risk.daily_loss_limit_pct.toFixed(1)}%</span></li>
    <li><span>Daily target</span><span>+${risk.daily_target_pct.toFixed(1)}%</span></li>
  `;

  renderWatchlist(settings.universe);
  if (document.activeElement !== $("capitalInput")) {
    $("capitalInput").value = settings.capital;
  }
  $("capitalGroup").style.opacity = status.mode === "live" ? "0.45" : "1";

  renderScanInfo(scan, settings);
  renderScanNote(status, risk, positions, signals);
  renderPositions(positions);
  renderSignals(signals);
  renderActivity(activity || []);

  $("lastUpdate").textContent = `updated ${data.updated_at}`;
  $("errText").textContent = status.halted_reason
    ? `· ${status.halted_reason}`
    : status.last_error
    ? `· broker: ${status.last_error}`
    : "";
}

function renderScanInfo(scan, settings) {
  const universe = settings.universe || [];
  const last = scan.last;
  const btn = $("btnScan");
  btn.disabled = Boolean(scan.scanning);
  btn.textContent = scan.scanning ? "Scanning…" : "Rescan market";

  if (scan.scanning) {
    $("scanMeta").textContent = "running";
    $("scanSummary").textContent = "Reading the NSE catalogue and scoring candidates…";
    return;
  }
  if (!last) {
    $("scanMeta").textContent = "never";
    $("scanSummary").textContent =
      "The bot scans every NSE stock and keeps the best tradeable uptrends. Press Rescan to run it now.";
    return;
  }
  const picked = (last.selected || []).length;
  $("scanMeta").textContent = `${picked} / ${(last.scanned || 0).toLocaleString("en-IN")} stocks`;
  $("scanSummary").textContent = `${last.message} · ${last.day || ""}`;
  const minutes = Number(settings.candle_minutes || 60);
  const timeframe = minutes >= 60 ? `${Math.round(minutes / 60)}-hour` : `${minutes}-minute`;
  $("signalSub").textContent = `Watching ${universe.length} screened stocks · ${timeframe} trend + RSI + breakout`;
}

function renderScanNote(status, risk, positions, signals) {
  const buys = signals.filter((s) => s.action === "BUY").length;
  const downtrend = signals.filter((s) => (s.reason || "").startsWith("Trend down")).length;
  const closed = !status.market_open && !status.simulated;

  let note;
  if (!signals.length) note = "Loading candles for the watchlist…";
  else if (buys) note = `${buys} setup(s) ready — the engine enters on its next scan.`;
  else if (closed) note = `Market closed (${status.market}). Positions are only opened during NSE hours, 09:15–15:30 IST.`;
  else if (!status.entries_allowed)
    note = "New entries are closed for today (cutoff 15:00 IST). Open positions stay protected by their stops; scanning and selecting resume tomorrow.";
  else if (positions.length >= risk.max_open_positions) note = `All ${risk.max_open_positions} position slots are in use.`;
  else if (status.halted_reason) note = `Paused for the day: ${status.halted_reason}.`;
  else if (downtrend)
    note = `${downtrend} of ${signals.length} symbols are below their EMA21 — this strategy only buys uptrends, so it waits.`;
  else note = "No confirmed setup yet — waiting for a close above the previous candle high.";

  $("scanNote").textContent = note;
}

function renderWatchlist(universe) {
  const host = $("watchlist");
  host.innerHTML = "";
  (universe || []).forEach((symbol) => {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = symbol;
    const remove = document.createElement("button");
    remove.textContent = "×";
    remove.title = `Remove ${symbol}`;
    remove.onclick = () => {
      const next = (ui.state?.settings.universe || []).filter((s) => s !== symbol);
      if (next.length === 0) return toast("Keep at least one symbol", "err");
      saveSettings({ universe: next });
    };
    chip.appendChild(remove);
    host.appendChild(chip);
  });
}

function renderPositions(positions) {
  const body = $("positionsBody");
  $("posCount").textContent = `${positions.length} open`;
  if (!positions.length) {
    body.innerHTML = `<tr class="empty-row"><td colspan="7">No open positions — the engine is waiting for a setup.</td></tr>`;
    return;
  }
  body.innerHTML = "";
  positions.forEach((p) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="sym">${p.symbol}<small>since ${p.opened_at.slice(11, 16)}</small></td>
      <td class="num">${p.qty}</td>
      <td class="num">${money(p.entry_price)}</td>
      <td class="num">${money(p.ltp)}</td>
      <td class="num down">${money(p.stop)}</td>
      <td class="num up">${money(p.target)}</td>
      <td class="num ${cls(p.pnl)}">${money(p.pnl, true)}<br><small>${pct(p.pnl_pct)}</small></td>
    `;
    body.appendChild(tr);
  });
}

function renderSignals(signals) {
  const host = $("signalList");
  if (!signals.length) {
    host.innerHTML = `<li class="empty-row">Waiting for the first scan…</li>`;
    return;
  }
  host.innerHTML = "";
  [...signals]
    .sort((a, b) => (b.score || 0) - (a.score || 0))
    .forEach((s) => {
      const li = document.createElement("li");

      const left = document.createElement("div");
      left.className = "sig-row";
      const sym = document.createElement("span");
      sym.className = "sym";
      sym.textContent = s.symbol;
      const price = document.createElement("span");
      price.className = "sig-price";
      price.textContent = s.ltp ? money(s.ltp) : "—";
      left.append(sym, price);

      const right = document.createElement("div");
      right.className = "sig-right";
      const tag = document.createElement("span");
      tag.className = `tag ${s.action === "BUY" ? "buy" : s.action === "EXIT" ? "sell" : "hold"}`;
      tag.textContent = s.action;
      const score = document.createElement("span");
      score.className = "sig-score";
      score.textContent = `${Math.round(s.score || 0)}%`;
      right.append(tag, score);

      const reason = document.createElement("div");
      reason.className = "sig-reason";
      reason.textContent = s.reason;

      li.append(left, right, reason);
      host.appendChild(li);
    });
}

function renderActivity(activity) {
  const host = $("activityList");
  if (!activity.length) {
    host.innerHTML = `<li class="empty-row">No activity yet.</li>`;
    return;
  }
  host.innerHTML = "";
  activity.forEach((item) => {
    const li = document.createElement("li");
    const time = document.createElement("span");
    time.className = "time";
    time.textContent = item.ts.slice(11, 16);
    const msg = document.createElement("span");
    msg.className = `lvl-${item.level}`;
    msg.textContent = item.message;
    li.append(time, msg);
    host.appendChild(li);
  });
}

function renderTrades() {
  const body = $("tradesBody");
  if (!ui.trades.length) {
    body.innerHTML = `<tr class="empty-row"><td colspan="7">No trades yet.</td></tr>`;
    return;
  }
  body.innerHTML = "";
  ui.trades.forEach((t) => {
    const tr = document.createElement("tr");
    const pnl = t.pnl === null || t.pnl === undefined ? "—" : money(t.pnl, true);
    tr.innerHTML = `
      <td class="mono" style="color:var(--muted-2)">${t.ts.slice(5, 16)}</td>
      <td><span class="tag ${t.side === "BUY" ? "buy" : "sell"}">${t.side}</span></td>
      <td class="sym">${t.symbol}</td>
      <td class="num">${t.qty}</td>
      <td class="num">${money(t.price)}</td>
      <td class="num ${t.pnl == null ? "neutral" : cls(t.pnl)}">${pnl}</td>
      <td style="color:var(--muted-2);white-space:normal;max-width:320px">${t.reason || ""}</td>
    `;
    body.appendChild(tr);
  });
}

/* --------------------------------------------------------------- controls */
function wireControls() {
  $("btnRun").addEventListener("click", () => {
    const state = ui.state?.status.state;
    control(state === "running" ? "pause" : state === "paused" ? "resume" : "start");
  });

  $("btnFlatten").addEventListener("click", () => {
    if (!confirm("Square off every open position at the current market price?")) return;
    control("close_all");
  });

  document.querySelectorAll("#modeGroup .seg").forEach((btn) => {
    btn.addEventListener("click", () => {
      const mode = btn.dataset.mode;
      if (ui.state?.status.mode === mode) return;
      if (mode === "live") {
        const ok = confirm(
          "Switch to LIVE trading?\n\nOrders will be sent to your Groww account with real money.\n" +
            "The engine must be stopped first, and current positions are cleared from the dashboard."
        );
        if (!ok) return;
        control("mode_live");
      } else {
        control("mode_paper");
      }
    });
  });

  document.querySelectorAll("#riskGroup .seg").forEach((btn) => {
    btn.addEventListener("click", () => saveSettings({ risk_level: btn.dataset.risk }));
  });

  $("btnAddSymbol").addEventListener("click", addSymbol);
  $("symbolInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter") addSymbol();
  });

  $("btnApplyCapital").addEventListener("click", () => {
    const value = Number($("capitalInput").value);
    if (!Number.isFinite(value) || value <= 0) return toast("Enter a valid capital amount", "err");
    saveSettings({ capital: value });
  });

  $("btnScan").addEventListener("click", async () => {
    try {
      const data = await api("/api/scan", { method: "POST" });
      if (data.result) toast(data.result.message, data.result.ok ? "ok" : "err");
      if (data.status) render(data);
    } catch (error) {
      toast(`Scan failed: ${error.message}`, "err");
    }
  });

  $("btnResetPaper").addEventListener("click", () => {
    if (!confirm("Reset the paper account? This clears paper trades and positions.")) return;
    control("reset_paper");
  });
}

function addSymbol() {
  const input = $("symbolInput");
  const symbol = input.value.trim().toUpperCase().replace(/[^A-Z0-9&-]/g, "");
  if (!symbol) return;
  const current = ui.state?.settings.universe || [];
  if (current.includes(symbol)) {
    input.value = "";
    return toast(`${symbol} is already in the watchlist`, "err");
  }
  input.value = "";
  saveSettings({ universe: [...current, symbol] });
}

/* -------------------------------------------------------------------- boot */
async function boot() {
  wireControls();
  try {
    render(await api("/api/state"));
  } catch (error) {
    toast(`Cannot reach the engine: ${error.message}`, "err");
  }
  loadTrades();
  setInterval(loadTrades, 5000);
  connect();
}

boot();
