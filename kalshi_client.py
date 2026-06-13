"""Kalshi Trade API v2 client with RSA-PSS request signing.

Kalshi authenticates each request with three headers:
  KALSHI-ACCESS-KEY        the API key ID
  KALSHI-ACCESS-TIMESTAMP  current time in milliseconds
  KALSHI-ACCESS-SIGNATURE  base64( RSA-PSS-SHA256( timestamp + METHOD + path ) )

The signed path includes the API prefix (/trade-api/v2/...) but excludes
query parameters.
"""

import base64
import logging
import os
import time
import uuid
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv

load_dotenv()

DEFAULT_BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"

log = logging.getLogger("kalshi")


def cents_to_dollars(cents: int) -> str:
    """65 -> "0.6500" (dollar string format used by the API since March 2026)."""
    return f"{cents / 100:.4f}"


def dollars_to_cents(dollar_string) -> int | None:
    """Convert an API price to integer cents.

    Accepts dollar strings ("0.6500" -> 65) and, for backward compatibility,
    values that are already integer cents (65 -> 65). Returns None for None.
    """
    if dollar_string is None:
        return None
    if isinstance(dollar_string, int):
        return dollar_string
    return round(float(dollar_string) * 100)


def _normalize_market_prices(market: dict) -> dict:
    """Normalize yes_bid / yes_ask on a market dict to integer cents, in place."""
    for field in ("yes_bid", "yes_ask"):
        if field in market:
            market[field] = dollars_to_cents(market[field])
    return market


class KalshiAPIError(Exception):
    """Raised when the Kalshi API returns a non-2xx response."""

    def __init__(self, status_code, body):
        self.status_code = status_code
        self.body = body
        super().__init__(f"Kalshi API error {status_code}: {body}")


class KalshiClient:
    def __init__(self, key_id=None, private_key_path=None, base_url=None):
        self.key_id = key_id or os.environ.get("KALSHI_KEY_ID")
        if not self.key_id:
            raise ValueError("KALSHI_KEY_ID is not set")

        key_content = os.environ.get("KALSHI_PRIVATE_KEY_CONTENT")
        if key_content:
            # Railway / cloud: key supplied as an env var string.
            # Normalize escaped newlines that some platforms introduce.
            pem_bytes = key_content.replace("\\n", "\n").encode("utf-8")
            self._private_key = serialization.load_pem_private_key(
                pem_bytes, password=None
            )
        else:
            key_path = private_key_path or os.environ.get("KALSHI_PRIVATE_KEY_PATH")
            if not key_path or not os.path.isfile(key_path):
                raise ValueError(
                    "No private key found. Set KALSHI_PRIVATE_KEY_CONTENT (cloud) "
                    f"or KALSHI_PRIVATE_KEY_PATH (local). Got path: {key_path!r}"
                )
            with open(key_path, "rb") as f:
                self._private_key = serialization.load_pem_private_key(
                    f.read(), password=None
                )

        self.base_url = (
            base_url or os.environ.get("KALSHI_BASE_URL", DEFAULT_BASE_URL)
        ).rstrip("/")
        # Path prefix that must be included in the signed message,
        # e.g. "/trade-api/v2"
        self._path_prefix = urlparse(self.base_url).path
        self._session = requests.Session()

    # ------------------------------------------------------------------ auth

    def _sign(self, timestamp_ms: str, method: str, path: str) -> str:
        message = f"{timestamp_ms}{method}{path}".encode("utf-8")
        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def _auth_headers(self, method: str, path: str) -> dict:
        timestamp_ms = str(int(time.time() * 1000))
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": self._sign(
                timestamp_ms, method, self._path_prefix + path
            ),
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------ http

    def _request(self, method: str, path: str, params=None, json=None):
        url = self.base_url + path
        resp = self._session.request(
            method,
            url,
            params=params,
            json=json,
            headers=self._auth_headers(method, path),
            timeout=30,
        )
        if not resp.ok:
            raise KalshiAPIError(resp.status_code, resp.text)
        return resp.json()

    # ------------------------------------------------------- portfolio

    def get_balance(self) -> dict:
        """GET /portfolio/balance — balance is returned in cents."""
        return self._request("GET", "/portfolio/balance")

    def get_positions(self, ticker=None, limit=100) -> dict:
        """GET /portfolio/positions."""
        params = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        return self._request("GET", "/portfolio/positions", params=params)

    def create_order(
        self,
        ticker: str,
        side: str,  # "yes" | "no"
        action: str,  # "buy" | "sell"
        count: int,
        order_type: str = "limit",  # "limit" | "market"
        yes_price: int | None = None,  # limit price in cents (1-99)
        no_price: int | None = None,
        client_order_id: str | None = None,
    ) -> dict:
        """POST /portfolio/orders.

        For limit orders set exactly one of yes_price / no_price.
        Prices are integer cents (a contract settles at $1.00 = 100 cents).
        """
        body = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "type": order_type,
            "client_order_id": client_order_id or str(uuid.uuid4()),
        }
        if yes_price is not None:
            body["yes_price"] = yes_price
        if no_price is not None:
            body["no_price"] = no_price
        return self._request("POST", "/portfolio/orders", json=body)

    def get_orders(self, ticker=None, status=None) -> dict:
        """GET /portfolio/orders."""
        params = {}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status
        return self._request("GET", "/portfolio/orders", params=params)

    # --------------------------------------------------------- markets

    def get_markets(self, limit=100, cursor=None, event_ticker=None,
                    series_ticker=None, status=None, mve_filter=None) -> dict:
        """GET /markets.

        mve_filter: 'exclude' to omit multivariate (KXMVE) markets,
        'only' to return only those.
        """
        params = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if event_ticker:
            params["event_ticker"] = event_ticker
        if series_ticker:
            params["series_ticker"] = series_ticker
        if status:
            params["status"] = status
        if mve_filter:
            params["mve_filter"] = mve_filter
        return self._request("GET", "/markets", params=params)

    def get_market(self, ticker: str) -> dict:
        """GET /markets/{ticker} — single market with current yes/no prices.

        yes_bid / yes_ask are normalized to integer cents regardless of
        whether the API returns dollar strings or integers.
        """
        data = self._request("GET", f"/markets/{ticker}")
        if isinstance(data.get("market"), dict):
            _normalize_market_prices(data["market"])
        return data

    def get_trades(self, ticker=None, min_ts=None, max_ts=None,
                   limit=1000, cursor=None) -> dict:
        """GET /markets/trades — public executed-trade history.

        min_ts / max_ts are Unix timestamps in seconds.
        """
        params = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if min_ts:
            params["min_ts"] = min_ts
        if max_ts:
            params["max_ts"] = max_ts
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", "/markets/trades", params=params)

    def get_market_orderbook(self, ticker: str) -> dict:
        """GET /markets/{ticker}/orderbook — best yes_bid / yes_ask in integer cents.

        The book lists YES bids and NO bids. The best YES ask is implied by
        the best NO bid: yes_ask = 100 - best_no_bid.
        """
        data = self._request("GET", f"/markets/{ticker}/orderbook")
        log.info("Raw orderbook response (%s): %s", ticker, str(data)[:200])
        # New format: orderbook_fp.yes_dollars / no_dollars with
        # ["price_string", "count_string"] levels. Old: orderbook.yes / no.
        book_fp = data.get("orderbook_fp")
        if book_fp:
            yes_levels = book_fp.get("yes_dollars") or []
            no_levels = book_fp.get("no_dollars") or []
        else:
            book = data.get("orderbook") or {}
            yes_levels = book.get("yes") or []
            no_levels = book.get("no") or []
        yes_bid = max(
            (dollars_to_cents(lvl[0]) for lvl in yes_levels), default=None
        )
        best_no_bid = max(
            (dollars_to_cents(lvl[0]) for lvl in no_levels), default=None
        )
        yes_ask = 100 - best_no_bid if best_no_bid is not None else None
        return {"yes_bid": yes_bid, "yes_ask": yes_ask}

    def get_all_active_markets(self, categories=None, max_markets=500) -> list:
        """Paginate through all active markets, optionally filtered by category list."""
        markets = []
        cursor = None
        logged_raw = False
        while len(markets) < max_markets:
            data = self.get_markets(limit=200, cursor=cursor, mve_filter="exclude")
            batch = data.get("markets", [])
            if not batch:
                break
            if not logged_raw:
                raw = batch[0].get("yes_ask")
                log.info(
                    "Raw yes_ask of first market fetched: %r (type=%s)",
                    raw, type(raw).__name__,
                )
                log.info("Sample tickers: %s", [m.get('ticker', '') for m in batch[:5]])
                logged_raw = True
            for m in batch:
                _normalize_market_prices(m)
            batch = [m for m in batch if m.get("status") == "active"]
            if categories:
                batch = [
                    m for m in batch
                    if not m.get("category")
                    or any(
                        cat.lower() in (m.get("category") or "").lower()
                        for cat in categories
                    )
                ]
            markets.extend(batch)
            cursor = data.get("cursor")
            if not cursor:
                break
        markets = markets[:max_markets]

        # Backfill missing prices from the orderbook. Limited to the first 20
        # non-KXMVE markets to bound the extra API calls; KXMVE multivariate
        # markets have no public orderbook.
        filled = 0
        candidates = [
            m for m in markets
            if m.get("ticker") and not m["ticker"].startswith("KXMVE")
        ][:20]
        for m in candidates:
            if m.get("yes_ask") is not None:
                continue
            try:
                ob = self.get_market_orderbook(m["ticker"])
            except KalshiAPIError as e:
                log.warning("Orderbook fetch failed for %s: %s", m["ticker"], e)
                continue
            if ob["yes_ask"] is not None:
                m["yes_ask"] = ob["yes_ask"]
            if ob["yes_bid"] is not None and m.get("yes_bid") is None:
                m["yes_bid"] = ob["yes_bid"]
            filled += 1
            log.info(
                "Orderbook backfill %s: yes_bid=%s yes_ask=%s",
                m["ticker"], ob["yes_bid"], ob["yes_ask"],
            )
        if filled:
            log.info("Backfilled prices from orderbook for %d markets", filled)
        return markets
