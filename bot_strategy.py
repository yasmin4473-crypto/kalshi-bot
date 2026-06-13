"""Multi-market threshold + AI trading bot for Kalshi v2.0.

Strategy (per market, prices in cents):
  - BUY YES  when YES ask < BUY_THRESHOLD
  - SELL YES when YES bid >= SELL_THRESHOLD
  - Stop-loss: sell immediately if price drops 50% below entry

Risk controls:
  - Max $20 total exposure across all markets
  - Max 3 open positions simultaneously
  - Daily loss limit: halt if total P&L < -$15

AI selection (OpenRouter/GPT-4o-mini):
  - Every 5 minutes, fetch all active markets and ask AI for top 3 picks
  - Automatically spawn per-market threads for AI-selected tickers
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

from kalshi_client import KalshiAPIError, KalshiClient

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("bot")

POLL_SECONDS = 2
NETWORK_RETRY_SECONDS = 10
WATCHDOG_SECONDS = 30
AI_REFRESH_SECONDS = 300
STOP_LOSS_RATIO = 0.5
MAX_EXPOSURE_CENTS = 2000
MAX_POSITIONS = 3
DAILY_LOSS_LIMIT_CENTS = -1500
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
AI_MODEL = "openai/gpt-4o-mini"
GNEWS_URL = "https://gnews.io/api/v4/search"
NEWS_CACHE_SECONDS = 900  # reuse headlines across AI scans to stay under the free 100 req/day tier
NEWS_QUERY = "sports betting prediction markets NBA MLB NFL"
CATEGORIES = ["Sports", "Crypto", "Politics", "Economics"]


class RiskManager:
    """Thread-safe risk controls: exposure cap, position limit, daily loss halt."""

    def __init__(self):
        self._lock = threading.Lock()
        self._positions: dict = {}  # ticker -> {count, entry_price}
        self.daily_pnl_cents = 0
        self.halted = False

    def can_open(self, ticker: str):
        """Returns (allowed: bool, reason: str)."""
        with self._lock:
            if self.halted:
                return False, "daily loss limit reached"
            open_count = len({t for t in self._positions if t != ticker})
            if open_count >= MAX_POSITIONS:
                return False, f"max {MAX_POSITIONS} open positions"
            exposure = sum(p["count"] * p["entry_price"] for p in self._positions.values())
            if exposure >= MAX_EXPOSURE_CENTS:
                return False, f"max exposure ${MAX_EXPOSURE_CENTS / 100:.0f} reached"
            return True, ""

    def record_buy(self, ticker: str, count: int, price: int):
        with self._lock:
            self._positions[ticker] = {"count": count, "entry_price": price}

    def record_sell(self, ticker: str, count: int, sell_price: int) -> int:
        """Record a sell and return P&L in cents."""
        with self._lock:
            if ticker not in self._positions:
                return 0
            entry = self._positions.pop(ticker)
            pnl = (sell_price - entry["entry_price"]) * count
            self.daily_pnl_cents += pnl
            if self.daily_pnl_cents <= DAILY_LOSS_LIMIT_CENTS:
                self.halted = True
                log.warning(
                    "Daily loss limit reached ($%.2f). Bot halted.",
                    self.daily_pnl_cents / 100,
                )
            return pnl

    def get_status(self) -> dict:
        with self._lock:
            exposure = sum(
                p["count"] * p["entry_price"] for p in self._positions.values()
            )
            return {
                "open_positions": len(self._positions),
                "total_exposure_cents": exposure,
                "daily_pnl_cents": self.daily_pnl_cents,
                "halted": self.halted,
            }

    def reset_daily(self):
        with self._lock:
            self.daily_pnl_cents = 0
            self.halted = False


class MarketWorker:
    """Monitors and trades a single Kalshi market in its own thread at 2s intervals."""

    def __init__(
        self,
        client: KalshiClient,
        ticker: str,
        risk_manager: RiskManager,
        buy_threshold: int,
        sell_threshold: int,
        order_count: int,
    ):
        self.client = client
        self.ticker = ticker
        self.risk_manager = risk_manager
        self.buy_threshold = buy_threshold
        self.sell_threshold = sell_threshold
        self.order_count = order_count

        self._stop_event = threading.Event()
        self._thread = None

        self.running = False
        self.last_price = None
        self.last_check = None
        self.last_error = None
        self.trade_log: list = []
        self.current_pnl_cents = 0
        self._entry_price = None

    def start(self):
        if self.running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"worker-{self.ticker}"
        )
        self._thread.start()
        self.running = True
        log.info("Worker started: %s", self.ticker)

    def stop(self):
        self._stop_event.set()
        self.running = False
        log.info("Worker stop requested: %s", self.ticker)

    def status(self) -> dict:
        return {
            "ticker": self.ticker,
            "running": self.running,
            "last_price": self.last_price,
            "last_check": self.last_check,
            "last_error": self.last_error,
            "current_pnl_cents": self.current_pnl_cents,
            "entry_price": self._entry_price,
            "trade_log": self.trade_log[-50:],
        }

    # ---- internal ----

    def _run(self):
        while not self._stop_event.is_set():
            wait = POLL_SECONDS
            try:
                self._tick()
                self.last_error = None
            except (requests.exceptions.Timeout,
                    requests.exceptions.ConnectionError) as e:
                self.last_error = str(e)
                wait = NETWORK_RETRY_SECONDS
                log.warning(
                    "Worker %s network error (%s); retrying in %ds",
                    self.ticker, type(e).__name__, NETWORK_RETRY_SECONDS,
                )
            except KalshiAPIError as e:
                self.last_error = str(e)
                log.error("Worker %s API error: %s", self.ticker, e)
            except Exception as e:
                self.last_error = str(e)
                log.exception("Worker %s unexpected error", self.ticker)
            self._stop_event.wait(wait)
        self.running = False

    def _tick(self):
        market = self.client.get_market(self.ticker).get("market", {})
        yes_bid = market.get("yes_bid")
        yes_ask = market.get("yes_ask")
        status = market.get("status")
        self.last_price = {"yes_bid": yes_bid, "yes_ask": yes_ask, "status": status}
        self.last_check = datetime.now(timezone.utc).isoformat()

        if status != "active":
            log.info("Market %s not active (status=%s); skipping", self.ticker, status)
            return
        if yes_bid is None or yes_ask is None:
            log.info("No quotes for %s yet; skipping", self.ticker)
            return

        held = self._yes_contracts_held()

        # Stop-loss: sell immediately if price fell 50% below entry
        if held > 0 and self._entry_price and yes_bid < int(self._entry_price * STOP_LOSS_RATIO):
            log.warning(
                "%s stop-loss: bid %dc < %.0f%% of entry %dc",
                self.ticker, yes_bid, STOP_LOSS_RATIO * 100, self._entry_price,
            )
            self._place("sell", held, yes_bid)
            return

        if held == 0 and 0 < yes_ask < self.buy_threshold:
            ok, reason = self.risk_manager.can_open(self.ticker)
            if ok:
                log.info(
                    "%s: ask %dc < threshold %dc -> buying %d",
                    self.ticker, yes_ask, self.buy_threshold, self.order_count,
                )
                self._place("buy", self.order_count, yes_ask)
            else:
                log.info("%s: would buy but risk block: %s", self.ticker, reason)
        elif held > 0 and yes_bid >= self.sell_threshold:
            log.info(
                "%s: bid %dc >= threshold %dc -> selling %d",
                self.ticker, yes_bid, self.sell_threshold, held,
            )
            self._place("sell", held, yes_bid)
        else:
            log.info("%s: bid=%sc ask=%sc held=%d — no action", self.ticker, yes_bid, yes_ask, held)

    def _yes_contracts_held(self) -> int:
        positions = self.client.get_positions(ticker=self.ticker)
        for pos in positions.get("market_positions", []):
            if pos.get("ticker") == self.ticker:
                return max(pos.get("position", 0), 0)
        return 0

    def _place(self, action: str, count: int, yes_price: int):
        order = self.client.create_order(
            ticker=self.ticker,
            side="yes",
            action=action,
            count=count,
            order_type="limit",
            yes_price=yes_price,
        )
        pnl_this_trade = None
        if action == "buy":
            self._entry_price = yes_price
            self.risk_manager.record_buy(self.ticker, count, yes_price)
        else:
            pnl_this_trade = self.risk_manager.record_sell(self.ticker, count, yes_price)
            self.current_pnl_cents += pnl_this_trade
            self._entry_price = None

        entry = {
            "time": datetime.now(timezone.utc).isoformat(),
            "ticker": self.ticker,
            "action": action,
            "count": count,
            "yes_price": yes_price,
            "pnl_cents": pnl_this_trade,
            "order": order.get("order", order),
        }
        self.trade_log.append(entry)
        log.info("Order placed: %s %s %d @ %dc", self.ticker, action, count, yes_price)


class NewsClient:
    """Fetches recent headlines from GNews, with caching to respect the
    free tier limit (100 requests/day)."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._http = requests.Session()
        self._cache: dict = {}  # query -> (fetched_at, headlines)

    def get_news(self, query: str, max_articles: int = 3) -> list:
        """Return a list of recent headline strings for the query."""
        cached = self._cache.get(query)
        if cached and time.time() - cached[0] < NEWS_CACHE_SECONDS:
            return cached[1]
        resp = self._http.get(
            GNEWS_URL,
            params={
                "q": query,
                "lang": "en",
                "max": max_articles,
                "apikey": self.api_key,
            },
            timeout=15,
        )
        resp.raise_for_status()
        articles = resp.json().get("articles", [])
        headlines = [
            a["title"] for a in articles if a.get("title")
        ][:max_articles]
        self._cache[query] = (time.time(), headlines)
        log.info("GNews: fetched %d headlines for %r", len(headlines), query)
        return headlines


class AISelector:
    """Queries OpenRouter/GPT-4o-mini for top market picks."""

    def __init__(self, api_key: str, news_client: "NewsClient | None" = None):
        self.api_key = api_key
        self.news_client = news_client
        self._http = requests.Session()
        self.recommendations: list = []
        self.last_update = None
        self.last_error = None

    def fetch_recommendations(self, markets: list) -> list:
        candidates = [
            {
                "ticker": m.get("ticker", ""),
                "title": m.get("title", ""),
                "yes_ask": m.get("yes_ask"),
            }
            for m in markets
            if m.get("status") == "active"
            and m.get("yes_ask") is not None
            and 1 <= m["yes_ask"] <= 99
        ][:20]

        log.info("AI candidates after 1-99c filter: %d", len(candidates))
        if not candidates:
            return []

        system_prompt = (
            "You are a prediction market analyst. Given these Kalshi markets and their "
            "current YES prices, identify the top 3 markets with the most favorable "
            "risk/reward ratio for buying YES contracts. "
            'Return JSON: {"picks": [{"ticker": "...", "reason": "...", "confidence": 0.0}]}'
        )

        if self.news_client:
            try:
                headlines = self.news_client.get_news(NEWS_QUERY)
            except Exception as e:
                headlines = []
                log.warning("News fetch failed (continuing without context): %s", e)
            if headlines:
                bullets = "\n".join(f"- {h}" for h in headlines)
                system_prompt += (
                    f"\n\nRecent news context:\n{bullets}\n\n"
                    "Use this context to make better predictions."
                )

        payload = {
            "model": AI_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": f"Markets:\n{json.dumps(candidates, indent=2)}",
                },
            ],
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "kalshi-bot",
        }
        resp = self._http.post(OPENROUTER_URL, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        log.info("Raw AI response: %s", content)
        parsed = json.loads(content)
        picks = parsed.get("picks", [])
        self.recommendations = picks[:3]
        self.last_update = datetime.now(timezone.utc).isoformat()
        self.last_error = None
        return self.recommendations

    def status(self) -> dict:
        return {
            "recommendations": self.recommendations,
            "last_update": self.last_update,
            "last_error": self.last_error,
        }


class Backtester:
    """Replays historical executed-trade prices through the threshold strategy.

    Downloads public trade history (GET /markets/trades) for the last N days
    and simulates the same rules the live bot uses:
      - buy YES when price < buy_threshold (one position at a time)
      - sell YES when price >= sell_threshold
      - stop-loss when price < 50% of entry
    Any position still open at the end is marked to the last seen price.
    """

    MAX_PAGES = 20  # 1000 trades/page — caps download at 20k prints

    def __init__(
        self,
        client: KalshiClient,
        ticker: str,
        buy_threshold: int,
        sell_threshold: int,
        order_count: int,
        days: int = 30,
    ):
        self.client = client
        self.ticker = ticker
        self.buy_threshold = buy_threshold
        self.sell_threshold = sell_threshold
        self.order_count = order_count
        self.days = days

    def fetch_history(self) -> list:
        """All executed trades for the ticker in the window, oldest first."""
        max_ts = int(time.time())
        min_ts = max_ts - self.days * 86400
        prints = []
        cursor = None
        for _ in range(self.MAX_PAGES):
            data = self.client.get_trades(
                ticker=self.ticker, min_ts=min_ts, max_ts=max_ts, cursor=cursor
            )
            batch = data.get("trades", [])
            if not batch:
                break
            prints.extend(batch)
            cursor = data.get("cursor")
            if not cursor:
                break
        prints.sort(key=lambda t: t.get("created_time", ""))
        log.info("Backtest %s: %d historical trades over %d days",
                 self.ticker, len(prints), self.days)
        return prints

    def run(self) -> dict:
        history = self.fetch_history()
        prices = [
            (t.get("created_time", ""), t["yes_price"])
            for t in history
            if t.get("yes_price") is not None
        ]

        entry_price = None
        entry_time = None
        completed: list[dict] = []

        for ts, price in prices:
            if entry_price is None:
                if 0 < price < self.buy_threshold:
                    entry_price = price
                    entry_time = ts
            else:
                stop = price < int(entry_price * STOP_LOSS_RATIO)
                take = price >= self.sell_threshold
                if stop or take:
                    completed.append({
                        "entry_time": entry_time,
                        "exit_time": ts,
                        "buy_price": entry_price,
                        "sell_price": price,
                        "count": self.order_count,
                        "pnl_cents": (price - entry_price) * self.order_count,
                        "exit_reason": "stop_loss" if stop else "take_profit",
                    })
                    entry_price = None

        open_marked = False
        if entry_price is not None and prices:
            last_ts, last_price = prices[-1]
            completed.append({
                "entry_time": entry_time,
                "exit_time": last_ts,
                "buy_price": entry_price,
                "sell_price": last_price,
                "count": self.order_count,
                "pnl_cents": (last_price - entry_price) * self.order_count,
                "exit_reason": "open_marked_to_last",
            })
            open_marked = True

        # Metrics
        total_pnl = 0
        peak = 0
        max_drawdown = 0
        wins = 0
        for tr in completed:
            total_pnl += tr["pnl_cents"]
            peak = max(peak, total_pnl)
            max_drawdown = max(max_drawdown, peak - total_pnl)
            if tr["pnl_cents"] > 0:
                wins += 1

        n = len(completed)
        return {
            "ticker": self.ticker,
            "days": self.days,
            "price_points": len(prices),
            "total_trades": n,
            "wins": wins,
            "losses": n - wins,
            "win_rate": round(wins / n, 3) if n else None,
            "total_pnl_cents": total_pnl,
            "max_drawdown_cents": max_drawdown,
            "open_position_marked_to_last": open_marked,
            "buy_threshold": self.buy_threshold,
            "sell_threshold": self.sell_threshold,
            "order_count": self.order_count,
            "trades": completed,
        }


def print_backtest_report(report: dict):
    n = report["total_trades"]
    wr = f"{report['win_rate'] * 100:.1f}%" if report["win_rate"] is not None else "n/a"
    print()
    print("=" * 56)
    print(f"  BACKTEST REPORT — {report['ticker']}  (last {report['days']} days)")
    print("=" * 56)
    print(f"  Price points replayed : {report['price_points']}")
    print(f"  Strategy              : buy<{report['buy_threshold']}c, "
          f"sell>={report['sell_threshold']}c, stop-loss 50%")
    print(f"  Total trades          : {n}")
    print(f"  Win rate              : {wr}  ({report['wins']}W / {report['losses']}L)")
    print(f"  Total P&L             : ${report['total_pnl_cents'] / 100:+.2f}")
    print(f"  Max drawdown          : ${report['max_drawdown_cents'] / 100:.2f}")
    if report["open_position_marked_to_last"]:
        print("  Note: final open position marked to last seen price")
    print("=" * 56)
    for tr in report["trades"]:
        print(f"  {tr['entry_time'][:19]}  buy {tr['buy_price']:>2}c -> "
              f"sell {tr['sell_price']:>2}c  ({tr['exit_reason']})  "
              f"${tr['pnl_cents'] / 100:+.2f}")
    print()


class MultiMarketBot:
    """
    v2.0: Monitors multiple Kalshi markets simultaneously.

    - One thread per market, all polling at 2-second intervals
    - AI (OpenRouter/GPT-4o-mini) selects top markets every 5 minutes
    - Shared RiskManager enforces exposure, position, and daily-loss limits
    - Backward-compatible with v1 TradingBot constructor and .status() shape
    """

    def __init__(
        self,
        client: KalshiClient,
        ticker=None,
        order_count=None,
        buy_threshold=None,
        sell_threshold=None,
        poll_seconds=None,  # kept for API compat; ignored — always 2s in v2
    ):
        self.client = client
        self.ticker = ticker or os.environ.get("BOT_MARKET_TICKER", "")
        self.order_count = order_count or int(os.environ.get("BOT_ORDER_COUNT", "10"))
        self.buy_threshold = buy_threshold or int(
            os.environ.get("BOT_BUY_THRESHOLD", "30")
        )
        self.sell_threshold = sell_threshold or int(
            os.environ.get("BOT_SELL_THRESHOLD", "70")
        )
        self.poll_seconds = POLL_SECONDS

        self.risk_manager = RiskManager()
        gnews_key = os.environ.get("GNEWS_API_KEY", "")
        news_client = NewsClient(gnews_key) if gnews_key else None
        if not news_client:
            log.info("No GNEWS_API_KEY set; AI will run without news context")
        openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
        self.ai_selector = (
            AISelector(openrouter_key, news_client=news_client)
            if openrouter_key else None
        )

        self._workers: dict = {}
        self._workers_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._ai_thread = None
        self._watchdog_thread = None

        self.running = False
        self.last_error = None

    # ---- lifecycle ----

    def start(self) -> bool:
        with self._workers_lock:
            if self.running:
                return False
            self._stop_event.clear()
            self.running = True

        if self.ticker:
            self._start_worker(self.ticker)

        if self.ai_selector:
            self._ai_thread = threading.Thread(
                target=self._ai_loop, daemon=True, name="ai-selector"
            )
            self._ai_thread.start()
            log.info("AI market selector started")
        else:
            log.info("No OPENROUTER_API_KEY set; AI selector disabled")

        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, daemon=True, name="watchdog"
        )
        self._watchdog_thread.start()

        log.info(
            "MultiMarketBot v2.0 started (buy<%dc, sell>=%dc, %d contracts)",
            self.buy_threshold, self.sell_threshold, self.order_count,
        )
        return True

    def stop(self) -> bool:
        with self._workers_lock:
            if not self.running:
                return False
            self._stop_event.set()
            for w in self._workers.values():
                w.stop()
            self.running = False
        log.info("MultiMarketBot stopped")
        return True

    def status(self) -> dict:
        with self._workers_lock:
            markets_status = [w.status() for w in self._workers.values()]

        all_trades = []
        total_pnl = 0
        for ws in markets_status:
            all_trades.extend(ws.get("trade_log", []))
            total_pnl += ws.get("current_pnl_cents", 0)

        all_trades.sort(key=lambda x: x.get("time", ""), reverse=True)

        risk = self.risk_manager.get_status()
        ai_status = self.ai_selector.status() if self.ai_selector else {
            "recommendations": [], "last_update": None, "last_error": None
        }

        # Legacy v1 compat fields (uses primary ticker's worker if set)
        primary = self._workers.get(self.ticker) if self.ticker else None
        return {
            # v1 fields
            "running": self.running,
            "ticker": self.ticker,
            "order_count": self.order_count,
            "buy_threshold": self.buy_threshold,
            "sell_threshold": self.sell_threshold,
            "poll_seconds": self.poll_seconds,
            "last_price": primary.last_price if primary else None,
            "last_check": primary.last_check if primary else None,
            "last_error": primary.last_error if primary else self.last_error,
            "trade_log": all_trades[:50],
            # v2 fields
            "markets": markets_status,
            "total_pnl_cents": total_pnl,
            "risk": risk,
            "ai": ai_status,
        }

    # ---- internal ----

    def _start_worker(self, ticker: str):
        with self._workers_lock:
            if not self.running or ticker in self._workers:
                return
            w = MarketWorker(
                client=self.client,
                ticker=ticker,
                risk_manager=self.risk_manager,
                buy_threshold=self.buy_threshold,
                sell_threshold=self.sell_threshold,
                order_count=self.order_count,
            )
            w.start()
            self._workers[ticker] = w

    def _watchdog_loop(self):
        """Restart any worker whose thread died while the bot is running."""
        while not self._stop_event.is_set():
            self._stop_event.wait(WATCHDOG_SECONDS)
            if self._stop_event.is_set():
                break
            with self._workers_lock:
                dead = [w for w in self._workers.values() if not w.running]
            for w in dead:
                log.warning("Watchdog: worker %s is down; restarting", w.ticker)
                w.start()

    def _ai_loop(self):
        log.info("AI selection loop started; running first scan immediately")
        while not self._stop_event.is_set():
            try:
                markets = self.client.get_all_active_markets()
                log.info("Fetched %d active markets for AI selection", len(markets))
                picks = self.ai_selector.fetch_recommendations(markets)
                for pick in picks:
                    ticker = (pick.get("ticker") or "").strip()
                    if ticker:
                        self._start_worker(ticker)
                log.info(
                    "AI selected %d markets: %s",
                    len(picks),
                    [p.get("ticker") for p in picks],
                )
            except Exception as e:
                if self.ai_selector:
                    self.ai_selector.last_error = str(e)
                log.exception("AI selection error")
            self._stop_event.wait(AI_REFRESH_SECONDS)


# Backward-compatible alias — app.py imports TradingBot and this keeps it working
TradingBot = MultiMarketBot


if __name__ == "__main__":
    backtest_mode = os.environ.get("BACKTEST_MODE", "").lower() in ("1", "true", "yes")

    if backtest_mode:
        ticker = os.environ.get("BOT_MARKET_TICKER", "")
        if not ticker:
            raise SystemExit("BACKTEST_MODE requires BOT_MARKET_TICKER in .env")
        bt = Backtester(
            client=KalshiClient(),
            ticker=ticker,
            buy_threshold=int(os.environ.get("BOT_BUY_THRESHOLD", "30")),
            sell_threshold=int(os.environ.get("BOT_SELL_THRESHOLD", "70")),
            order_count=int(os.environ.get("BOT_ORDER_COUNT", "10")),
            days=int(os.environ.get("BACKTEST_DAYS", "30")),
        )
        print_backtest_report(bt.run())
    else:
        bot = TradingBot(KalshiClient())
        bot.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            bot.stop()
            print("Bot stopped.")
