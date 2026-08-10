from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests


FAPI = "https://fapi.binance.com"
SAO_PAULO = timezone(timedelta(hours=-3))


def pct(entry: float, price: float) -> float:
    return (price - entry) / entry * 100.0


def load_signals(database: Path, start_ms: int) -> list[dict[str, Any]]:
    db = sqlite3.connect(database)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT timestamp_ms,symbol,signal_json FROM executions "
        "WHERE ok=1 AND timestamp_ms>=? ORDER BY timestamp_ms", (start_ms,),
    ).fetchall()
    db.close()
    result = []
    for row in rows:
        signal = json.loads(row["signal_json"] or "{}")
        if signal.get("setup") != "DUMP_EXHAUSTION_RECLAIM_V1":
            continue
        result.append({
            "timestamp_ms": int(row["timestamp_ms"]), "symbol": str(row["symbol"]),
            "entry": float(signal["entry_price"]),
            "entry_atr_pct": float(((signal.get("evidence") or {}).get("entry") or {}).get("atr14_pct") or 0.0),
        })
    return list({(row["timestamp_ms"], row["symbol"]): row for row in result}.values())


def get_1m(session: requests.Session, symbol: str, start_ms: int, end_ms: int) -> list[list[Any]]:
    result, cursor = [], start_ms
    while cursor < end_ms:
        response = session.get(FAPI + "/fapi/v1/klines", params={
            "symbol": symbol, "interval": "1m", "startTime": cursor,
            "endTime": end_ms, "limit": 1000,
        }, timeout=20)
        response.raise_for_status()
        rows = response.json()
        if not rows:
            break
        result.extend(rows)
        cursor = int(rows[-1][0]) + 60_000
    return result


def fixed(rows: list[list[Any]], entry: float, stop_pct: float, target_pct: float) -> dict[str, Any]:
    stop, target, peak = entry * (1 - stop_pct / 100), entry * (1 + target_pct / 100), 0.0
    for row in rows:
        high, low = float(row[2]), float(row[3])
        peak = max(peak, pct(entry, high))
        if low <= stop:
            return {"outcome": "STOP", "exit_pct": -stop_pct, "mfe_pct": peak}
        if high >= target:
            return {"outcome": "TARGET", "exit_pct": target_pct, "mfe_pct": peak}
    close = float(rows[-1][4])
    return {"outcome": "OPEN", "exit_pct": pct(entry, close), "mfe_pct": peak}


def logical(rows: list[list[Any]], entry: float, atr_pct: float, stop_pct: float,
            activation_pct: float, minimum_lock_pct: float, atr_multiple: float,
            min_giveback_pct: float, max_giveback_pct: float) -> dict[str, Any]:
    stop, peak, armed, trail = entry * (1 - stop_pct / 100), 0.0, False, None
    giveback = max(min_giveback_pct, min(max_giveback_pct, atr_pct * atr_multiple))
    for row in rows:
        high, low = float(row[2]), float(row[3])
        if low <= stop and not armed:
            return {"outcome": "STOP", "exit_pct": -stop_pct, "mfe_pct": peak,
                    "giveback_pct": giveback, "trail_pct": trail}
        # A previously armed trail is evaluated before using this minute's new high.
        if armed and trail is not None and low <= entry * (1 + trail / 100):
            return {"outcome": "LOGICAL_TRAIL", "exit_pct": trail, "mfe_pct": peak,
                    "giveback_pct": giveback, "trail_pct": trail}
        peak = max(peak, pct(entry, high))
        if peak >= activation_pct:
            armed = True
            trail = max(minimum_lock_pct, peak - giveback)
    close = float(rows[-1][4])
    return {"outcome": "OPEN_ARMED" if armed else "OPEN", "exit_pct": pct(entry, close),
            "mfe_pct": peak, "giveback_pct": giveback, "trail_pct": trail}


def aggregate(trades: list[dict[str, Any]], key: str, cost_pct: float) -> dict[str, Any]:
    closed = [row for row in trades if row[key]["outcome"] not in {"OPEN", "OPEN_ARMED"}]
    values = [row[key]["exit_pct"] - cost_pct for row in closed]
    wins, losses = [v for v in values if v > 0], [v for v in values if v < 0]
    return {
        "closed": len(closed), "open": len(trades) - len(closed),
        "net_pct_sum_closed": round(sum(values), 4),
        "mean_net_pct_closed": round(sum(values) / len(values), 4) if values else 0.0,
        "win_rate_pct_closed": round(len(wins) / len(values) * 100, 2) if values else 0.0,
        "profit_factor_closed": round(sum(wins) / -sum(losses), 3) if losses else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--date", default=datetime.now(SAO_PAULO).date().isoformat())
    parser.add_argument("--cost-pct", type=float, default=0.14)
    args = parser.parse_args()
    day = datetime.fromisoformat(args.date).replace(tzinfo=SAO_PAULO)
    start_ms, now_ms = int(day.timestamp() * 1000), int(time.time() * 1000)
    signals = load_signals(args.database, start_ms)
    session, trades = requests.Session(), []
    for signal in signals:
        # Begin at the first complete minute after the observed signal to avoid pre-entry look-ahead.
        path_start = ((signal["timestamp_ms"] // 60_000) + 1) * 60_000
        rows = get_1m(session, signal["symbol"], path_start, now_ms)
        if not rows:
            continue
        baseline = fixed(rows, signal["entry"], 1.5, 3.0)
        dynamic = logical(rows, signal["entry"], signal["entry_atr_pct"], 1.5, 3.0, 3.0, 1.25, 0.65, 1.40)
        trades.append({**signal, "fixed_tp3": baseline, "logical_exit": dynamic})
    report = {
        "date": args.date, "venue": "Binance Futures production public 1m candles",
        "assumptions": {
            "stop_pct": 1.5, "activation_profit_pct": 3.0,
            "minimum_locked_profit_pct": 3.0, "atr_giveback_multiple": 1.25,
            "giveback_bounds_pct": [0.65, 1.40], "round_trip_cost_pct": args.cost_pct,
            "intrabar_rule": "previous trail first; new minute high updates next trail",
        },
        "signals": len(trades),
        "summary": {
            "fixed_tp3": aggregate(trades, "fixed_tp3", args.cost_pct),
            "logical_exit": aggregate(trades, "logical_exit", args.cost_pct),
        },
        "trades": trades,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
