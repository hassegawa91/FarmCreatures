from __future__ import annotations

import argparse
import json
import random
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BASE_URL = "https://fapi.binance.com"
FIVE_MINUTES_MS = 300_000


def get_json(path: str, params: dict[str, Any] | None = None, attempts: int = 6) -> Any:
    query = urllib.parse.urlencode(params or {})
    url = f"{BASE_URL}{path}" + (f"?{query}" if query else "")
    for attempt in range(attempts):
        request = urllib.request.Request(url, headers={"User-Agent": "v10-research/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=35) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code not in {418, 429, 500, 502, 503, 504} or attempt + 1 == attempts:
                raise
            retry_after = float(exc.headers.get("Retry-After") or 0)
            time.sleep(max(retry_after, min(30.0, 2**attempt)) + random.random())
        except (TimeoutError, urllib.error.URLError):
            if attempt + 1 == attempts:
                raise
            time.sleep(min(30.0, 2**attempt) + random.random())
    raise RuntimeError("request retry loop exhausted")


def paginated(path: str, symbol: str, start_ms: int, end_ms: int, limit: int) -> list[Any]:
    rows: list[Any] = []
    cursor = start_ms
    while cursor <= end_ms:
        page_end_ms = min(end_ms, cursor + (limit - 1) * FIVE_MINUTES_MS)
        page = get_json(path, {
            "symbol": symbol,
            "period" if path.startswith("/futures/data/") else "interval": "5m",
            "startTime": cursor,
            "endTime": page_end_ms,
            "limit": limit,
        })
        if not page:
            break
        def row_timestamp(row: Any) -> int:
            return int(row[0] if isinstance(row, list) else row["timestamp"])

        eligible = [row for row in page if cursor <= row_timestamp(row) <= end_ms]
        if not eligible:
            break
        rows.extend(eligible)
        timestamp = max(row_timestamp(row) for row in eligible)
        next_cursor = timestamp + FIVE_MINUTES_MS
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        time.sleep(0.04)
    return rows


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;
        CREATE TABLE IF NOT EXISTS research_klines_5m (
            symbol TEXT NOT NULL, timestamp_ms INTEGER NOT NULL,
            open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
            base_volume REAL NOT NULL, quote_volume REAL NOT NULL, trades INTEGER NOT NULL,
            taker_buy_base REAL NOT NULL, taker_buy_quote REAL NOT NULL,
            PRIMARY KEY(symbol,timestamp_ms)
        );
        CREATE TABLE IF NOT EXISTS research_oi_5m (
            symbol TEXT NOT NULL, timestamp_ms INTEGER NOT NULL,
            open_interest REAL NOT NULL, open_interest_value REAL NOT NULL,
            PRIMARY KEY(symbol,timestamp_ms)
        );
        CREATE TABLE IF NOT EXISTS research_global_lsr_5m (
            symbol TEXT NOT NULL, timestamp_ms INTEGER NOT NULL,
            long_short_ratio REAL NOT NULL, long_account REAL NOT NULL, short_account REAL NOT NULL,
            PRIMARY KEY(symbol,timestamp_ms)
        );
        CREATE TABLE IF NOT EXISTS research_taker_5m (
            symbol TEXT NOT NULL, timestamp_ms INTEGER NOT NULL,
            buy_sell_ratio REAL NOT NULL, buy_volume REAL NOT NULL, sell_volume REAL NOT NULL,
            PRIMARY KEY(symbol,timestamp_ms)
        );
        CREATE TABLE IF NOT EXISTS research_collection_runs (
            run_id TEXT PRIMARY KEY, started_utc TEXT NOT NULL, finished_utc TEXT,
            start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL, symbols_json TEXT NOT NULL,
            status TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}'
        );
        """
    )


def active_usdt_symbols() -> set[str]:
    info = get_json("/fapi/v1/exchangeInfo")
    return {
        item["symbol"] for item in info["symbols"]
        if item.get("status") == "TRADING"
        and item.get("quoteAsset") == "USDT"
        and item.get("contractType") == "PERPETUAL"
    }


def top_symbols(count: int) -> list[str]:
    active = active_usdt_symbols()
    tickers = get_json("/fapi/v1/ticker/24hr")
    ranked = sorted(
        (item for item in tickers if item.get("symbol") in active),
        key=lambda item: float(item.get("quoteVolume") or 0), reverse=True,
    )
    return [item["symbol"] for item in ranked[:count]]


def collect_symbol(symbol: str, start_ms: int, end_ms: int) -> dict[str, list[Any]]:
    jobs = {
        "klines": ("/fapi/v1/klines", 1500),
        "oi": ("/futures/data/openInterestHist", 500),
        "lsr": ("/futures/data/globalLongShortAccountRatio", 500),
        "taker": ("/futures/data/takerlongshortRatio", 500),
    }
    result: dict[str, list[Any]] = {}
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(paginated, path, symbol, start_ms, end_ms, limit): name
            for name, (path, limit) in jobs.items()
        }
        for future in as_completed(futures):
            result[futures[future]] = future.result()
    return result


def store_symbol(connection: sqlite3.Connection, symbol: str, data: dict[str, list[Any]]) -> dict[str, int]:
    with connection:
        connection.executemany(
            "INSERT OR REPLACE INTO research_klines_5m VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            [(symbol, int(r[0]), *map(float, r[1:5]), float(r[5]), float(r[7]), int(r[8]), float(r[9]), float(r[10])) for r in data["klines"]],
        )
        connection.executemany(
            "INSERT OR REPLACE INTO research_oi_5m VALUES(?,?,?,?)",
            [(symbol, int(r["timestamp"]), float(r["sumOpenInterest"]), float(r["sumOpenInterestValue"])) for r in data["oi"]],
        )
        connection.executemany(
            "INSERT OR REPLACE INTO research_global_lsr_5m VALUES(?,?,?,?,?)",
            [(symbol, int(r["timestamp"]), float(r["longShortRatio"]), float(r["longAccount"]), float(r["shortAccount"])) for r in data["lsr"]],
        )
        connection.executemany(
            "INSERT OR REPLACE INTO research_taker_5m VALUES(?,?,?,?,?)",
            [(symbol, int(r["timestamp"]), float(r["buySellRatio"]), float(r["buyVol"]), float(r["sellVol"])) for r in data["taker"]],
        )
    return {name: len(rows) for name, rows in data.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description="Coleta dados publicos alinhaveis para pesquisa, sem tocar na engine.")
    parser.add_argument("database", type=Path)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--symbols", help="Lista separada por virgula")
    group.add_argument("--top", type=int, help="Top N contratos USDT por quote volume 24h")
    parser.add_argument("--days", type=float, default=29.0)
    args = parser.parse_args()

    now_ms = int(time.time() * 1000)
    end_ms = now_ms - now_ms % FIVE_MINUTES_MS - 1
    start_ms = end_ms - int(args.days * 86_400_000)
    symbols = [item.strip().upper() for item in args.symbols.split(",")] if args.symbols else top_symbols(args.top)
    run_id = datetime.now(timezone.utc).strftime("binance-%Y%m%dT%H%M%SZ")

    connection = sqlite3.connect(args.database, timeout=60)
    initialize(connection)
    connection.execute(
        "INSERT INTO research_collection_runs(run_id,started_utc,start_ms,end_ms,symbols_json,status) VALUES(?,?,?,?,?,?)",
        (run_id, datetime.now(timezone.utc).isoformat(), start_ms, end_ms, json.dumps(symbols), "RUNNING"),
    )
    connection.commit()
    details: dict[str, Any] = {}
    try:
        for index, symbol in enumerate(symbols, 1):
            started = time.perf_counter()
            counts = store_symbol(connection, symbol, collect_symbol(symbol, start_ms, end_ms))
            details[symbol] = {"counts": counts, "elapsed_seconds": round(time.perf_counter() - started, 2)}
            print(json.dumps({"progress": f"{index}/{len(symbols)}", "symbol": symbol, **details[symbol]}), flush=True)
        status = "OK"
    except Exception as exc:
        status = "ERROR"
        details["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        connection.execute(
            "UPDATE research_collection_runs SET finished_utc=?,status=?,details_json=? WHERE run_id=?",
            (datetime.now(timezone.utc).isoformat(), status, json.dumps(details, ensure_ascii=False), run_id),
        )
        connection.commit()
        connection.close()


if __name__ == "__main__":
    main()
