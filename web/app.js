const state = {
  data: null,
  formatter: new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2 }),
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

function money(value) {
  const n = Number(value || 0);
  return `Rs ${state.formatter.format(n)}`;
}

function signedMoney(value) {
  const n = Number(value || 0);
  const sign = n > 0 ? "+" : "";
  return `${sign}${money(n)}`;
}

function pct(value) {
  const n = Number(value || 0);
  return `${n > 0 ? "+" : ""}${n.toFixed(2)}%`;
}

function cls(value) {
  return Number(value || 0) >= 0 ? "gain" : "loss";
}

function text(value, fallback = "--") {
  if (value === null || value === undefined || value === "") return fallback;
  return String(value);
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}

async function refresh() {
  $("#refresh-btn").disabled = true;
  try {
    const [data, logs] = await Promise.all([
      request("/api/status"),
      request("/api/logs?limit=220"),
    ]);
    state.data = data;
    render(data);
    $("#logs-box").textContent = logs.lines.join("\n") || "No log lines yet.";
  } catch (error) {
    $("#operation-output").textContent = `Refresh failed: ${error.message}`;
  } finally {
    $("#refresh-btn").disabled = false;
  }
}

function render(data) {
  const p = data.portfolio;
  $("#last-refresh").textContent = `Last refresh ${new Date(data.now).toLocaleString()}`;
  $("#pnl-ticker").className = `pnl-value ${cls(p.pnl)}`;
  $("#pnl-ticker").textContent = `${signedMoney(p.pnl)} (${pct(p.pnl_pct)})`;
  $("#pnl-ticker-detail").textContent = `${money(p.total_value)} total value | ${signedMoney(p.unrealised_pnl)} unrealised`;
  $("#metric-total").textContent = money(p.total_value);
  $("#metric-pnl").innerHTML = `<span class="${cls(p.pnl)}">${signedMoney(p.pnl)} (${pct(p.pnl_pct)})</span>`;
  $("#metric-cash").textContent = money(p.cash);
  $("#metric-market").textContent = `${money(p.market_value)} marked in positions`;
  $("#metric-open").textContent = `${p.positions.length} position${p.positions.length === 1 ? "" : "s"}`;
  $("#metric-unrealised").innerHTML = `<span class="${cls(p.unrealised_pnl)}">${signedMoney(p.unrealised_pnl)} unrealised</span>`;
  $("#metric-trades").textContent = data.trades.length;
  $("#metric-winrate").textContent = winRate(data.trades);

  $("#bot-dot").classList.toggle("running", data.bot.running);
  $("#bot-state").textContent = data.bot.running ? "Bot running" : "Bot stopped";
  $("#bot-pid").textContent = data.bot.pid ? `PID ${data.bot.pid}` : "No dashboard-owned PID";

  renderPositions(p.positions);
  renderTrades(data.trades);
  renderStates(data.latest_states);
  drawEquity(data.snapshots, p.starting_capital);
}

function winRate(trades) {
  const closed = trades.filter((t) => typeof t.pnl === "number");
  if (!closed.length) return "No closed trades";
  const wins = closed.filter((t) => t.pnl > 0).length;
  return `${Math.round((wins / closed.length) * 100)}% win rate`;
}

function renderPositions(positions) {
  $("#position-count").textContent = `${positions.length} open`;
  const body = $("#positions-body");
  body.innerHTML = "";
  if (!positions.length) {
    body.innerHTML = `<tr><td colspan="8">No open positions.</td></tr>`;
    return;
  }
  positions.forEach((pos) => {
    const row = document.createElement("tr");
    row.innerHTML = `
      <td><strong>${text(pos.ticker)}</strong></td>
      <td><span class="badge">${text(pos.side)}</span></td>
      <td>${Number(pos.shares || 0).toFixed(3)}</td>
      <td>${money(pos.entry_price)}</td>
      <td>${money(pos.current_price)}</td>
      <td class="${cls(pos.unrealised_pnl)}">${signedMoney(pos.unrealised_pnl)}</td>
      <td>${money(pos.effective_stop_price)}</td>
      <td>${money(pos.take_profit_price)}</td>
    `;
    body.appendChild(row);
  });
}

function renderTrades(trades) {
  $("#trade-count").textContent = `${trades.length} trades`;
  const body = $("#trades-body");
  body.innerHTML = "";
  trades.slice(-40).reverse().forEach((trade) => {
    const pnl = typeof trade.pnl === "number" ? signedMoney(trade.pnl) : "Open";
    const row = document.createElement("tr");
    row.innerHTML = `
      <td>${text(trade.date)}</td>
      <td><strong>${text(trade.ticker)}</strong></td>
      <td><span class="badge">${text(trade.action)}</span></td>
      <td>${Number(trade.shares || 0).toFixed(3)}</td>
      <td>${money(trade.price)}</td>
      <td>${money(trade.notional)}</td>
      <td class="${typeof trade.pnl === "number" ? cls(trade.pnl) : ""}">${pnl}</td>
    `;
    body.appendChild(row);
  });
}

function renderStates(states) {
  $("#state-count").textContent = `${states.length} run${states.length === 1 ? "" : "s"}`;
  const list = $("#state-list");
  list.innerHTML = "";
  if (!states.length) {
    list.innerHTML = `<p>No full state logs found yet.</p>`;
    return;
  }
  states.forEach((item, index) => {
    const el = document.createElement("article");
    el.className = "state-item";
    const panels = {
      decision: item.final_trade_decision,
      trader: item.trader_investment_decision,
      research: item.investment_debate_state?.history || item.investment_debate_state?.judge_decision,
      risk: item.risk_debate_state?.history || item.risk_debate_state?.judge_decision,
      market: item.reports?.market,
      news: item.reports?.news,
      fundamentals: item.reports?.fundamentals,
    };
    el.innerHTML = `
      <div class="state-summary">
        <h4>${text(item.ticker)} <span class="badge">${text(item.trade_date, "latest")}</span></h4>
        <small>${text(item.relative_path)}</small>
        <div class="state-tabs">
          ${Object.keys(panels).map((key) => `<button data-state="${index}" data-panel="${key}">${key}</button>`).join("")}
        </div>
      </div>
      <pre class="state-text">${escapeHtml(panels.decision || "No final decision text.")}</pre>
    `;
    list.appendChild(el);
    el.querySelectorAll("button").forEach((button) => {
      button.addEventListener("click", () => {
        el.querySelector(".state-text").textContent = panels[button.dataset.panel] || "No text captured for this panel.";
      });
    });
  });
}

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function drawEquity(snapshots, startingCapital) {
  const canvas = $("#equity-chart");
  const ctx = canvas.getContext("2d");
  const width = canvas.width;
  const height = canvas.height;
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, width, height);

  const points = snapshots.map((s) => Number(s.total_value || 0)).filter(Boolean);
  if (!points.length) points.push(Number(startingCapital || 0));
  const min = Math.min(...points, Number(startingCapital || 0));
  const max = Math.max(...points, Number(startingCapital || 0));
  const pad = Math.max((max - min) * 0.18, 1000);
  const low = min - pad;
  const high = max + pad;

  ctx.strokeStyle = "#d8dee6";
  ctx.lineWidth = 1;
  for (let i = 0; i < 4; i += 1) {
    const y = 28 + i * ((height - 56) / 3);
    ctx.beginPath();
    ctx.moveTo(42, y);
    ctx.lineTo(width - 18, y);
    ctx.stroke();
  }

  const xFor = (i) => 42 + (i / Math.max(points.length - 1, 1)) * (width - 70);
  const yFor = (v) => height - 28 - ((v - low) / (high - low || 1)) * (height - 56);

  ctx.strokeStyle = "#94a3b8";
  ctx.setLineDash([4, 5]);
  const baseY = yFor(startingCapital || points[0]);
  ctx.beginPath();
  ctx.moveTo(42, baseY);
  ctx.lineTo(width - 18, baseY);
  ctx.stroke();
  ctx.setLineDash([]);

  ctx.strokeStyle = "#0f766e";
  ctx.lineWidth = 3;
  ctx.beginPath();
  points.forEach((value, index) => {
    const x = xFor(index);
    const y = yFor(value);
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();

  points.forEach((value, index) => {
    ctx.fillStyle = value >= startingCapital ? "#0f766e" : "#b42318";
    ctx.beginPath();
    ctx.arc(xFor(index), yFor(value), 4, 0, Math.PI * 2);
    ctx.fill();
  });

  ctx.fillStyle = "#667085";
  ctx.font = "13px system-ui";
  ctx.fillText(money(high), 42, 18);
  ctx.fillText(money(low), 42, height - 8);
}

async function runAction(action, button) {
  button.disabled = true;
  $("#operation-output").textContent = `Running ${action}...`;
  try {
    const result = await request("/api/control", {
      method: "POST",
      body: JSON.stringify({ action }),
    });
    $("#operation-output").textContent = result.output || result.message || result.error || JSON.stringify(result, null, 2);
    await refresh();
  } catch (error) {
    $("#operation-output").textContent = `Operation failed: ${error.message}`;
  } finally {
    button.disabled = false;
  }
}

async function runBot(action, button) {
  button.disabled = true;
  try {
    const result = await request("/api/bot", {
      method: "POST",
      body: JSON.stringify({ action }),
    });
    $("#operation-output").textContent = result.message || JSON.stringify(result, null, 2);
    await refresh();
  } catch (error) {
    $("#operation-output").textContent = `Bot command failed: ${error.message}`;
  } finally {
    button.disabled = false;
  }
}

$("#refresh-btn").addEventListener("click", refresh);
$$("[data-action]").forEach((button) => button.addEventListener("click", () => runAction(button.dataset.action, button)));
$$("[data-bot]").forEach((button) => button.addEventListener("click", () => runBot(button.dataset.bot, button)));

refresh();
setInterval(refresh, 30000);
