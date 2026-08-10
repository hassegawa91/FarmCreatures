from __future__ import annotations

import argparse
import json
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests


FAPI = "https://fapi.binance.com"
SAO_PAULO = timezone(timedelta(hours=-3))


def directional(direction: str, entry: float, price: float) -> float:
    raw = (price - entry) / entry * 100.0
    return raw if direction == "LONG" else -raw


def load_signals(database: Path, start_ms: int) -> list[dict[str, Any]]:
    db = sqlite3.connect(database)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT id,timestamp_ms,symbol,direction,signal_json FROM executions "
        "WHERE ok=1 AND timestamp_ms>=? ORDER BY timestamp_ms", (start_ms,),
    ).fetchall()
    db.close()
    result: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        signal = json.loads(row["signal_json"] or "{}")
        setup = str(signal.get("setup") or "UNKNOWN")
        entry, stop, target = (float(signal.get(key) or 0.0) for key in (
            "entry_price", "stop_price", "target_price",
        ))
        direction = str(row["direction"])
        if min(entry, stop, target) <= 0 or directional(direction, entry, target) <= 0:
            continue
        item = {
            "execution_id": int(row["id"]), "timestamp_ms": int(row["timestamp_ms"]),
            "symbol": str(row["symbol"]), "direction": direction, "setup": setup,
            "entry": entry, "stop": stop, "target": target,
            "stop_pct": abs(directional(direction, entry, stop)),
            "target_pct": directional(direction, entry, target),
            "entry_atr_pct": float(((signal.get("evidence") or {}).get("entry") or {}).get("atr14_pct") or 0.0),
        }
        result[(item["timestamp_ms"], item["symbol"], direction, setup)] = item
    return list(result.values())


def testnet_realized(database: Path, start_ms: int) -> dict[str, dict[str, Any]]:
    db = sqlite3.connect(database)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT e.signal_json,r.net_pct,r.net_pnl FROM execution_results r "
        "JOIN executions e ON e.id=r.execution_id WHERE r.closed_at_ms>=?", (start_ms,),
    ).fetchall()
    db.close()
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        setup = str(json.loads(row["signal_json"] or "{}").get("setup") or "UNKNOWN")
        grouped[setup].append(row)
    result = {}
    for setup, selected in grouped.items():
        pcts = [float(row["net_pct"]) for row in selected]
        pnls = [float(row["net_pnl"]) for row in selected]
        wins, losses = [v for v in pnls if v > 0], [v for v in pnls if v < 0]
        result[setup] = {
            "closed": len(selected), "net_pnl_usdt": round(sum(pnls), 4),
            "net_pct_sum": round(sum(pcts), 4),
            "win_rate_pct": round(sum(v > 0 for v in pnls) / len(pnls) * 100, 2),
            "profit_factor": round(sum(wins) / -sum(losses), 3) if losses else None,
        }
    return result


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


def touched(direction: str, row: list[Any], price: float, kind: str) -> bool:
    high, low = float(row[2]), float(row[3])
    if kind == "STOP":
        return low <= price if direction == "LONG" else high >= price
    return high >= price if direction == "LONG" else low <= price


def fixed(rows: list[list[Any]], signal: dict[str, Any]) -> dict[str, Any]:
    peak = 0.0
    for row in rows:
        favorable = float(row[2]) if signal["direction"] == "LONG" else float(row[3])
        peak = max(peak, directional(signal["direction"], signal["entry"], favorable))
        if touched(signal["direction"], row, signal["stop"], "STOP"):
            return {"outcome": "STOP", "gross_pct": -signal["stop_pct"], "mfe_pct": peak}
        if touched(signal["direction"], row, signal["target"], "TARGET"):
            return {"outcome": "TARGET", "gross_pct": signal["target_pct"], "mfe_pct": peak}
    close = float(rows[-1][4])
    return {"outcome": "OPEN", "gross_pct": directional(signal["direction"], signal["entry"], close), "mfe_pct": peak}


def logical(rows: list[list[Any]], signal: dict[str, Any]) -> dict[str, Any]:
    peak, armed, trail_pct = 0.0, False, None
    giveback = max(0.65, min(1.40, signal["entry_atr_pct"] * 1.25))
    for row in rows:
        if not armed and touched(signal["direction"], row, signal["stop"], "STOP"):
            return {"outcome": "STOP", "gross_pct": -signal["stop_pct"], "mfe_pct": peak}
        if armed and trail_pct is not None:
            trail_price = (
                signal["entry"] * (1 + trail_pct / 100)
                if signal["direction"] == "LONG"
                else signal["entry"] * (1 - trail_pct / 100)
            )
            high, low = float(row[2]), float(row[3])
            trail_hit = low <= trail_price if signal["direction"] == "LONG" else high >= trail_price
            if trail_hit:
                return {"outcome": "LOGICAL_TRAIL", "gross_pct": trail_pct,
                        "mfe_pct": peak, "giveback_pct": giveback}
        favorable = float(row[2]) if signal["direction"] == "LONG" else float(row[3])
        peak = max(peak, directional(signal["direction"], signal["entry"], favorable))
        if peak >= signal["target_pct"]:
            armed = True
            trail_pct = max(signal["target_pct"], peak - giveback)
    close = float(rows[-1][4])
    return {"outcome": "OPEN_ARMED" if armed else "OPEN",
            "gross_pct": directional(signal["direction"], signal["entry"], close),
            "mfe_pct": peak, "giveback_pct": giveback, "trail_pct": trail_pct}


def stats(trades: list[dict[str, Any]], model: str, cost_pct: float) -> dict[str, Any]:
    closed = [row for row in trades if row[model]["outcome"] not in {"OPEN", "OPEN_ARMED"}]
    nets = [row[model]["gross_pct"] - cost_pct for row in closed]
    wins, losses = [v for v in nets if v > 0], [v for v in nets if v < 0]
    return {
        "signals": len(trades), "closed": len(closed), "open": len(trades) - len(closed),
        "net_pct_sum_closed": round(sum(nets), 4),
        "mean_net_pct_closed": round(sum(nets) / len(nets), 4) if nets else 0.0,
        "win_rate_pct_closed": round(len(wins) / len(nets) * 100, 2) if nets else 0.0,
        "profit_factor_closed": round(sum(wins) / -sum(losses), 3) if losses else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--date", default=datetime.now(SAO_PAULO).date().isoformat())
    parser.add_argument("--cost-pct", type=float, default=0.14)
    args = parser.parse_args()
    start_ms = int(datetime.fromisoformat(args.date).replace(tzinfo=SAO_PAULO).timestamp() * 1000)
    now_ms, session = int(time.time() * 1000), requests.Session()
    signals, trials = load_signals(args.database, start_ms), []
    for signal in signals:
        path_start = ((signal["timestamp_ms"] // 60_000) + 1) * 60_000
        rows = get_1m(session, signal["symbol"], path_start, now_ms)
        if rows:
            trials.append({**signal, "fixed_tp": fixed(rows, signal), "logical_tp": logical(rows, signal)})
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in trials:
        grouped[row["setup"]].append(row)
    report = {
        "date": args.date, "venue": "Binance Futures production public 1m candles",
        "assumptions": {
            "same_signals_and_native_stop_target": True, "round_trip_cost_pct": args.cost_pct,
            "logical_rule": "native target becomes minimum protected floor; ATR trail captures excess",
            "intrabar_rule": "stop first before activation; previous trail before new minute peak",
        },
        "testnet_realized": testnet_realized(args.database, start_ms),
        "by_strategy": {
            setup: {
                "native_target_pct_values": sorted({round(row["target_pct"], 6) for row in rows}),
                "fixed_tp": stats(rows, "fixed_tp", args.cost_pct),
                "logical_tp": stats(rows, "logical_tp", args.cost_pct),
            }
            for setup, rows in sorted(grouped.items())
        },
        "overall": {
            "fixed_tp": stats(trials, "fixed_tp", args.cost_pct),
            "logical_tp": stats(trials, "logical_tp", args.cost_pct),
        },
        "trades": trials,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("date", "testnet_realized", "by_strategy", "overall")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
