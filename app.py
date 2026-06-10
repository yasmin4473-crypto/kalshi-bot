"""Flask server: JSON API + dashboard for Kalshi trading bot v2.0."""

import os

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template_string, request

from bot_strategy import Backtester, TradingBot
from kalshi_client import KalshiAPIError, KalshiClient

load_dotenv()

app = Flask(__name__)
client = KalshiClient()
bot = TradingBot(client)


@app.errorhandler(KalshiAPIError)
def handle_kalshi_error(e):
    return jsonify({"error": e.body, "status_code": e.status_code}), 502


# ------------------------------------------------------------------ API

@app.get("/api/balance")
def api_balance():
    return jsonify(client.get_balance())


@app.get("/api/markets")
def api_markets():
    params = {
        "limit": request.args.get("limit", 100, type=int),
        "status": request.args.get("status"),
        "event_ticker": request.args.get("event_ticker"),
        "series_ticker": request.args.get("series_ticker"),
        "cursor": request.args.get("cursor"),
    }
    return jsonify(client.get_markets(**{k: v for k, v in params.items() if v}))


@app.get("/api/positions")
def api_positions():
    return jsonify(client.get_positions(ticker=request.args.get("ticker")))


@app.get("/api/orders")
def api_orders():
    return jsonify(
        client.get_orders(
            ticker=request.args.get("ticker"),
            status=request.args.get("status"),
        )
    )


@app.post("/api/orders")
def api_create_order():
    body = request.get_json(force=True)
    required = ("ticker", "side", "action", "count")
    missing = [f for f in required if f not in body]
    if missing:
        return jsonify({"error": f"missing fields: {', '.join(missing)}"}), 400
    return jsonify(
        client.create_order(
            ticker=body["ticker"],
            side=body["side"],
            action=body["action"],
            count=int(body["count"]),
            order_type=body.get("type", "limit"),
            yes_price=body.get("yes_price"),
            no_price=body.get("no_price"),
            client_order_id=body.get("client_order_id"),
        )
    ), 201


# ------------------------------------------------------------------ bot

@app.get("/api/bot/status")
def api_bot_status():
    return jsonify(bot.status())


@app.post("/api/bot/start")
def api_bot_start():
    try:
        started = bot.start()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"running": True, "started": started})


@app.post("/api/bot/stop")
def api_bot_stop():
    stopped = bot.stop()
    return jsonify({"running": False, "stopped": stopped})


@app.get("/api/bot/markets")
def api_bot_markets():
    st = bot.status()
    return jsonify({"markets": st.get("markets", [])})


@app.get("/api/backtest")
def api_backtest():
    """Run a backtest synchronously. ?ticker=X required; ?days=30 optional."""
    ticker = request.args.get("ticker") or os.environ.get("BOT_MARKET_TICKER", "")
    if not ticker:
        return jsonify({"error": "ticker query param (or BOT_MARKET_TICKER) required"}), 400
    bt = Backtester(
        client=client,
        ticker=ticker,
        buy_threshold=bot.buy_threshold,
        sell_threshold=bot.sell_threshold,
        order_count=bot.order_count,
        days=request.args.get("days", 30, type=int),
    )
    return jsonify(bt.run())


@app.get("/api/bot/ai")
def api_bot_ai():
    st = bot.status()
    return jsonify(st.get("ai", {"recommendations": [], "last_update": None, "last_error": None}))


# ------------------------------------------------------------- dashboard

DASHBOARD = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Kalshi Bot v2.0</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: system-ui, sans-serif; margin: 0; padding: 2rem; background: #0f1117; color: #e6e6e6; }
  h1 { font-size: 1.4rem; margin: 0 0 1.5rem; }
  h2 { font-size: .75rem; text-transform: uppercase; letter-spacing: .06em; color: #8b90a0; margin: 1.75rem 0 .6rem; }
  .cards { display: flex; gap: .75rem; flex-wrap: wrap; margin-bottom: 1.5rem; }
  .card { background: #1a1d27; border-radius: 10px; padding: .9rem 1.3rem; min-width: 150px; }
  .card h3 { margin: 0 0 .25rem; font-size: .7rem; text-transform: uppercase; color: #8b90a0; }
  .card .v { font-size: 1.35rem; font-weight: 600; }
  button { background: #3b82f6; color: #fff; border: 0; border-radius: 6px;
           padding: .45rem 1rem; cursor: pointer; font-size: .85rem; margin-right: .4rem; }
  button.stop { background: #ef4444; }
  button:hover { opacity: .85; }
  table { width: 100%; border-collapse: collapse; background: #1a1d27; border-radius: 10px;
          overflow: hidden; margin-bottom: 1.5rem; font-size: .85rem; }
  th { background: #12151f; text-align: left; padding: .55rem 1rem;
       font-size: .7rem; text-transform: uppercase; letter-spacing: .05em; color: #8b90a0; }
  td { padding: .5rem 1rem; border-top: 1px solid #252836; }
  tr:hover td { background: #1e2235; }
  .ok   { color: #22c55e; }
  .bad  { color: #ef4444; }
  .warn { color: #f59e0b; }
  .muted { color: #8b90a0; }
  .badge { display: inline-block; padding: .15rem .45rem; border-radius: 4px;
           font-size: .7rem; font-weight: 700; letter-spacing: .04em; }
  .badge-green { background: #14532d; color: #22c55e; }
  .badge-red   { background: #7f1d1d; color: #ef4444; }
  .badge-blue  { background: #1e3a5f; color: #60a5fa; }
  .alert { background: #7f1d1d; border-radius: 8px; padding: .7rem 1.2rem;
           margin-bottom: 1rem; color: #fca5a5; display: none; font-size: .9rem; }
  #refresh-ts { font-size: .72rem; color: #8b90a0; margin-left: .75rem; font-weight: 400; }
  .ai-disabled { color: #8b90a0; font-size: .82rem; font-style: italic; }
</style>
</head>
<body>
<h1>Kalshi Trading Bot <span style="color:#3b82f6">v2.0</span><span id="refresh-ts"></span></h1>

<div class="cards">
  <div class="card"><h3>Balance</h3><div class="v" id="balance">…</div></div>
  <div class="card"><h3>Bot</h3><div class="v" id="botstate">…</div></div>
  <div class="card"><h3>Portfolio P&amp;L</h3><div class="v" id="total-pnl">…</div></div>
  <div class="card"><h3>Exposure</h3><div class="v" id="exposure">…</div></div>
  <div class="card"><h3>Positions</h3><div class="v" id="open-pos">…</div></div>
</div>

<p>
  <button onclick="botCmd('start')">Start Bot</button>
  <button class="stop" onclick="botCmd('stop')">Stop Bot</button>
</p>

<div id="halt-alert" class="alert">
  &#9888; Daily loss limit reached — bot halted. Stop and restart to reset.
</div>

<h2>Active Markets</h2>
<table>
  <thead>
    <tr>
      <th>Ticker</th>
      <th>YES Bid</th>
      <th>YES Ask</th>
      <th>Market Status</th>
      <th>Last Action</th>
      <th>P&amp;L</th>
    </tr>
  </thead>
  <tbody id="markets-body">
    <tr><td colspan="6" class="muted">No markets monitored yet — start the bot</td></tr>
  </tbody>
</table>

<h2>
  AI Recommendations
  <span class="muted" id="ai-ts" style="font-size:.72rem;text-transform:none;font-weight:400"></span>
</h2>
<table>
  <thead>
    <tr><th>Ticker</th><th>Reason</th><th>Confidence</th></tr>
  </thead>
  <tbody id="ai-body">
    <tr><td colspan="3" class="muted">Waiting for first AI scan…</td></tr>
  </tbody>
</table>

<h2>Trade Log</h2>
<table>
  <thead>
    <tr><th>Time</th><th>Ticker</th><th>Action</th><th>Price</th><th>P&amp;L</th></tr>
  </thead>
  <tbody id="trades-body">
    <tr><td colspan="5" class="muted">No trades yet</td></tr>
  </tbody>
</table>

<script>
async function refresh() {
  try {
    const [bal, st] = await Promise.all([
      fetch('/api/balance').then(r => r.json()),
      fetch('/api/bot/status').then(r => r.json()),
    ]);

    // Balance
    const balEl = document.getElementById('balance');
    balEl.textContent = bal.balance != null ? '$' + (bal.balance / 100).toFixed(2) : '—';

    // Bot state
    const bsEl = document.getElementById('botstate');
    bsEl.textContent = st.running ? 'RUNNING' : 'STOPPED';
    bsEl.className = 'v ' + (st.running ? 'ok' : 'bad');

    // Total P&L
    const pnl = st.total_pnl_cents || 0;
    const pnlEl = document.getElementById('total-pnl');
    pnlEl.textContent = (pnl >= 0 ? '+' : '') + '$' + (pnl / 100).toFixed(2);
    pnlEl.className = 'v ' + (pnl >= 0 ? 'ok' : 'bad');

    // Risk
    const risk = st.risk || {};
    const exp = risk.total_exposure_cents || 0;
    const expEl = document.getElementById('exposure');
    expEl.textContent = '$' + (exp / 100).toFixed(2) + ' / $20';
    expEl.className = 'v ' + (exp >= 1800 ? 'bad' : exp >= 1200 ? 'warn' : '');

    const posEl = document.getElementById('open-pos');
    posEl.textContent = (risk.open_positions || 0) + ' / 3';
    posEl.className = 'v ' + ((risk.open_positions || 0) >= 3 ? 'warn' : '');

    // Halt alert
    document.getElementById('halt-alert').style.display = risk.halted ? 'block' : 'none';

    // ---- Markets table ----
    const markets = st.markets || [];
    const mBody = document.getElementById('markets-body');
    if (!markets.length) {
      mBody.innerHTML = '<tr><td colspan="6" class="muted">No markets monitored yet — start the bot</td></tr>';
    } else {
      mBody.innerHTML = markets.map(m => {
        const p = m.last_price || {};
        const bid = p.yes_bid != null ? p.yes_bid + 'c' : '—';
        const ask = p.yes_ask != null ? p.yes_ask + 'c' : '—';
        const badge = m.running
          ? '<span class="badge badge-green">ACTIVE</span>'
          : '<span class="badge badge-red">STOPPED</span>';
        const log = m.trade_log || [];
        const last = log.length ? log[log.length - 1] : null;
        const lastAction = last
          ? '<span class="' + (last.action === 'buy' ? 'ok' : 'warn') + '">' + last.action.toUpperCase() + '</span>'
          : '<span class="muted">—</span>';
        const mpnl = m.current_pnl_cents || 0;
        const mpnlStr = mpnl !== 0
          ? '<span class="' + (mpnl >= 0 ? 'ok' : 'bad') + '">' + (mpnl >= 0 ? '+' : '') + '$' + (mpnl / 100).toFixed(2) + '</span>'
          : '<span class="muted">$0.00</span>';
        return '<tr><td><strong>' + m.ticker + '</strong></td><td>' + bid + '</td><td>' + ask +
               '</td><td>' + badge + '</td><td>' + lastAction + '</td><td>' + mpnlStr + '</td></tr>';
      }).join('');
    }

    // ---- AI recommendations ----
    const ai = st.ai || {};
    const recs = ai.recommendations || [];
    const aiBody = document.getElementById('ai-body');
    const aiTs = document.getElementById('ai-ts');
    if (ai.last_update) {
      aiTs.textContent = '(updated ' + new Date(ai.last_update).toLocaleTimeString() + ')';
    }
    if (!recs.length) {
      const msg = ai.last_error
        ? 'AI error: ' + ai.last_error
        : (ai.last_update ? 'No picks returned.' : 'Waiting for first AI scan (runs every 5 min)…');
      aiBody.innerHTML = '<tr><td colspan="3" class="muted">' + msg + '</td></tr>';
    } else {
      aiBody.innerHTML = recs.map(r => {
        const conf = r.confidence != null
          ? '<span class="' + (r.confidence >= 0.7 ? 'ok' : r.confidence >= 0.4 ? 'warn' : 'bad') + '">' + (r.confidence * 100).toFixed(0) + '%</span>'
          : '—';
        return '<tr><td><strong>' + (r.ticker || '—') + '</strong></td><td>' + (r.reason || '—') + '</td><td>' + conf + '</td></tr>';
      }).join('');
    }

    // ---- Trade log ----
    const trades = st.trade_log || [];
    const tBody = document.getElementById('trades-body');
    if (!trades.length) {
      tBody.innerHTML = '<tr><td colspan="5" class="muted">No trades yet</td></tr>';
    } else {
      tBody.innerHTML = trades.slice(0, 30).map(t => {
        const time = t.time ? new Date(t.time).toLocaleTimeString() : '—';
        const action = t.action
          ? '<span class="' + (t.action === 'buy' ? 'ok' : 'warn') + '">' + t.action.toUpperCase() + '</span>'
          : '—';
        const price = t.yes_price != null ? '$' + (t.yes_price / 100).toFixed(2) : '—';
        const tpnl = t.pnl_cents != null
          ? '<span class="' + (t.pnl_cents >= 0 ? 'ok' : 'bad') + '">' + (t.pnl_cents >= 0 ? '+' : '') + '$' + (t.pnl_cents / 100).toFixed(2) + '</span>'
          : '<span class="muted">—</span>';
        return '<tr><td class="muted">' + time + '</td><td>' + (t.ticker || '—') +
               '</td><td>' + action + '</td><td>' + price + '</td><td>' + tpnl + '</td></tr>';
      }).join('');
    }

    document.getElementById('refresh-ts').textContent =
      '— refreshed ' + new Date().toLocaleTimeString();

  } catch (e) {
    document.getElementById('refresh-ts').textContent = '— error: ' + e.message;
  }
}

async function botCmd(cmd) {
  const r = await fetch('/api/bot/' + cmd, { method: 'POST' });
  const j = await r.json();
  if (j.error) alert(j.error);
  refresh();
}

refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""


@app.get("/")
def dashboard():
    return render_template_string(DASHBOARD)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="127.0.0.1", port=port, debug=False)
