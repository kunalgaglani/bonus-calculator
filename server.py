#!/usr/bin/env python3
"""Serve the bonus analyzer and proxy daily prices.

Run: python3 server.py
Open: http://127.0.0.1:8765

The bonus amount is never accepted here. Price requests take a ticker and a lock date only.
"""

import json
import re
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = 8765
MARKET = ZoneInfo("America/New_York")
TICKER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9.\-]{0,9}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
YAHOO_UA = "Mozilla/5.0"
CACHE_TTL = 5 * 60
UPSTREAM_GAP = 5
cache_lock = threading.Lock()
price_cache = {}
last_upstream = 0.0


def fetch_prices(ticker, lock_date):
    year, month, day = (int(part) for part in lock_date.split("-"))
    start = datetime(year, month, day, tzinfo=MARKET)
    period1 = int(start.timestamp())
    period2 = int(datetime.now(MARKET).timestamp()) + 86400
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{ticker}?interval=1d&period1={period1}&period2={period2}"
    )
    request = Request(url, headers={"User-Agent": YAHOO_UA})
    with urlopen(request, timeout=15) as response:
        payload = json.load(response)

    chart = payload.get("chart") or {}
    if chart.get("error"):
        description = chart["error"].get("description") or "price lookup failed"
        raise RuntimeError(description)
    result = (chart.get("result") or [None])[0]
    if not result:
        raise RuntimeError("price lookup failed")

    meta = result.get("meta") or {}
    timestamps = result.get("timestamp") or []
    quotes = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quotes.get("close") or []

    by_date = {}
    for stamp, close in zip(timestamps, closes):
        if close is None:
            continue
        traded_on = datetime.fromtimestamp(stamp, MARKET).date().isoformat()
        if traded_on < lock_date:
            continue
        by_date[traded_on] = float(close)

    points = [{"date": traded_on, "close": by_date[traded_on]} for traded_on in sorted(by_date)]
    latest_price, latest_time = latest_quote(meta)
    if latest_price is not None and latest_time:
        live_day = datetime.fromtimestamp(latest_time, MARKET).date().isoformat()
        if live_day >= lock_date:
            if points and points[-1]["date"] == live_day:
                points[-1]["close"] = latest_price
            elif not points or points[-1]["date"] < live_day:
                points.append({"date": live_day, "close": latest_price})

    return {
        "ticker": meta.get("symbol") or ticker,
        "currency": meta.get("currency") or "USD",
        "points": points,
        "latest": {"price": latest_price, "time": latest_time},
    }


def get_prices(ticker, lock_date):
    """Return a quote, reusing the copy in memory for 5 minutes."""
    global last_upstream
    key = (ticker, lock_date)
    now = time.monotonic()
    with cache_lock:
        cached = price_cache.get(key)
        if cached and now - cached[0] < CACHE_TTL:
            return cached[1]
        if now - last_upstream < UPSTREAM_GAP:
            if cached:
                return cached[1]
            return None
        last_upstream = now
    payload = fetch_prices(ticker, lock_date)
    with cache_lock:
        price_cache[key] = (time.monotonic(), payload)
    return payload


def latest_quote(meta):
    candidates = []
    for price_key, time_key in (
        ("preMarketPrice", "preMarketTime"),
        ("regularMarketPrice", "regularMarketTime"),
        ("postMarketPrice", "postMarketTime"),
    ):
        price = meta.get(price_key)
        stamp = meta.get(time_key)
        if isinstance(price, (int, float)) and isinstance(stamp, (int, float)):
            candidates.append((float(stamp), float(price)))
    if not candidates:
        return None, None
    stamp, price = max(candidates)
    return price, int(stamp)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._file("index.html", "text/html; charset=utf-8")
        elif parsed.path == "/config.json":
            self._file("config.json", "application/json")
        elif parsed.path == "/api/prices":
            self._prices(parse_qs(parsed.query))
        elif parsed.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
        else:
            self._json(404, {"error": "not found"})

    def _prices(self, query):
        ticker = (query.get("ticker") or [""])[0].strip().upper()
        lock_date = (query.get("lockDate") or [""])[0].strip()
        if not TICKER_RE.fullmatch(ticker):
            self._json(400, {"error": "Enter a ticker symbol."})
            return
        if not DATE_RE.fullmatch(lock_date):
            self._json(400, {"error": "Enter a lock date."})
            return
        try:
            datetime.strptime(lock_date, "%Y-%m-%d")
        except ValueError:
            self._json(400, {"error": "Enter a lock date."})
            return
        try:
            self._json(200, get_prices(ticker, lock_date), cache_seconds=CACHE_TTL)
        except (HTTPError, URLError, TimeoutError, RuntimeError, json.JSONDecodeError):
            self._json(502, {"error": "Price lookup failed. Try again in a minute."})

    def _file(self, name, content_type):
        path = ROOT / name
        if not path.is_file():
            self._json(404, {"error": "not found"})
            return
        self._send(200, path.read_bytes(), content_type)

    def _json(self, status, payload, cache_seconds=0):
        self._send(status, json.dumps(payload).encode(), "application/json", cache_seconds)

    def _send(self, status, body, content_type, cache_seconds=0):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if cache_seconds:
            self.send_header("Cache-Control", f"public, max-age={cache_seconds}")
        else:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), urlparse(self.path).path))


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Live Bonus 2026 at http://{HOST}:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
