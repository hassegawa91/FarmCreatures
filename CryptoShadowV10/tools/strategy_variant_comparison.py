from __future__ import annotations

import argparse
import json
import sqlite3
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import requests


VARIANTS = {
    "DUMP_EXHAUSTION_RECLAIM_V1": [("SL1.2_TP3.0", 1.2, 3.0), ("SL1.5_TP3.0", 1.5, 3.0), ("SL2.0_TP3.0", 2.0, 3.0)],
    "BORDER_BREAKOUT_RETEST_LONG": [("SL0.65_TP1.43", 0.65, 1.43), ("SL0.8_TP1.6", 0.8, 1.6), ("SL1.0_TP2.0", 1.0, 2.0)],
    "BORDER_BREAKOUT_RETEST_SHORT": [("SL0.65_TP1.43", 0.65, 1.43), ("SL0.8_TP1.6", 0.8, 1.6), ("SL1.0_TP2.0", 1.0, 2.0)],
    "FAILED_BREAKOUT_REVERSAL_LONG": [("SL0.55_TP1.265", 0.55, 1.265), ("SL0.8_TP1.6", 0.8, 1.6), ("SL1.0_TP2.0", 1.0, 2.0)],
    "FAILED_BREAKOUT_REVERSAL_SHORT": [("SL0.55_TP1.265", 0.55, 1.265), ("SL0.8_TP1.6", 0.8, 1.6), ("SL1.0_TP2.0", 1.0, 2.0)],
}


def load(databases: list[Path]) -> list[dict[str, Any]]:
    unique: dict[tuple[int, str, str], dict[str, Any]] = {}
    for database in databases:
        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        for row in connection.execute("SELECT timestamp_ms,symbol,direction,ok,signal_json FROM executions WHERE ok=1"):
            signal = json.loads(row["signal_json"] or "{}")
            setup = str(signal.get("setup") or "UNKNOWN")
            entry = float(signal.get("entry_price") or 0.0)
            stop = float(signal.get("stop_price") or 0.0)
            target = float(signal.get("target_price") or 0.0)
            if min(entry, stop, target) <= 0 or setup not in VARIANTS:
                continue
            unique[(int(row["timestamp_ms"]), str(row["symbol"]), setup)] = {
                "timestamp_ms": int(row["timestamp_ms"]), "symbol": str(row["symbol"]),
                "direction": str(row["direction"]), "setup": setup, "entry": entry,
                "original_stop_pct": abs(entry - stop) / entry * 100.0,
                "original_target_pct": abs(target - entry) / entry * 100.0,
            }
        connection.close()
    return sorted(unique.values(), key=lambda row: row["timestamp_ms"])


def fetch(session: requests.Session, signal: dict[str, Any], end_ms: int) -> list[list[Any]]:
    response = session.get("https://fapi.binance.com/fapi/v1/klines", params={
        "symbol": signal["symbol"], "interval": "5m", "startTime": signal["timestamp_ms"],
        "endTime": end_ms, "limit": 1000,
    }, timeout=20)
    response.raise_for_status()
    return response.json()


def move(direction: str, entry: float, price: float) -> float:
    value = (price - entry) / entry * 100.0
    return value if direction == "LONG" else -value


def simulate(signal: dict[str, Any], rows: list[list[Any]], stop_pct: float, target_pct: float) -> dict[str, Any]:
    entry, direction = signal["entry"], signal["direction"]
    mfe, mae = 0.0, 0.0
    for row in rows:
        high, low = float(row[2]), float(row[3])
        mfe = max(mfe, move(direction, entry, high if direction == "LONG" else low))
        mae = min(mae, move(direction, entry, low if direction == "LONG" else high))
        stop_hit = low <= entry * (1 - stop_pct / 100) if direction == "LONG" else high >= entry * (1 + stop_pct / 100)
        target_hit = high >= entry * (1 + target_pct / 100) if direction == "LONG" else low <= entry * (1 - target_pct / 100)
        if stop_hit:
            return {"outcome": "STOP", "gross_pct": -stop_pct, "mfe_pct": mfe, "mae_pct": mae}
        if target_hit:
            return {"outcome": "TARGET", "gross_pct": target_pct, "mfe_pct": mfe, "mae_pct": mae}
    terminal = float(rows[-1][4])
    return {"outcome": "TIME", "gross_pct": move(direction, entry, terminal), "mfe_pct": mfe, "mae_pct": mae}


def summarize(rows: list[dict[str, Any]], cost: float) -> dict[str, Any]:
    net = [row["gross_pct"] - cost for row in rows]
    wins, losses = [x for x in net if x > 0], [x for x in net if x < 0]
    return {
        "n": len(rows), "wins": len(wins), "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(rows) * 100, 2) if rows else 0.0,
        "net_pct_sum": round(sum(net), 4),
        "mean_net_pct": round(sum(net) / len(rows), 4) if rows else 0.0,
        "profit_factor": round(sum(wins) / -sum(losses), 3) if losses else None,
        "outcomes": {key: sum(row["outcome"] == key for row in rows) for key in ("TARGET", "STOP", "TIME")},
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("databases", nargs="+", type=Path)
    parser.add_argument("--cost-pct", type=float, default=0.10)
    parser.add_argument("--max-horizon-minutes", type=int, default=240)
    args = parser.parse_args()
    signals = load(args.databases)
    now_ms = int(time.time() * 1000)
    session = requests.Session()
    paths = {
        (signal["timestamp_ms"], signal["symbol"], signal["setup"]): fetch(
            session, signal, min(now_ms, signal["timestamp_ms"] + args.max_horizon_minutes * 60_000)
        ) for signal in signals
    }
    report: dict[str, Any] = {"assumptions": {
        "venue": "Binance Futures production public 5m klines", "same_candle": "stop first",
        "cost_pct": args.cost_pct, "warning": "directional replay; Testnet fills may use a different basis",
    }, "setups": {}}
    for setup in VARIANTS:
        selected = [row for row in signals if row["setup"] == setup]
        report["setups"][setup] = {}
        for horizon in (60, 120, args.max_horizon_minutes):
            eligible = [row for row in selected if now_ms - row["timestamp_ms"] >= horizon * 60_000]
            variants = [("ORIGINAL", None, None), *VARIANTS[setup]]
            report["setups"][setup][str(horizon)] = {}
            for name, fixed_stop, fixed_target in variants:
                trials = []
                for signal in eligible:
                    path = [row for row in paths[(signal["timestamp_ms"], signal["symbol"], setup)] if int(row[0]) < signal["timestamp_ms"] + horizon * 60_000]
                    if not path:
                        continue
                    stop_pct = signal["original_stop_pct"] if fixed_stop is None else fixed_stop
                    target_pct = signal["original_target_pct"] if fixed_target is None else fixed_target
                    trials.append({**signal, "tested_stop_pct": stop_pct, "tested_target_pct": target_pct, **simulate(signal, path, stop_pct, target_pct)})
                report["setups"][setup][str(horizon)][name] = {**summarize(trials, args.cost_pct), "trades": trials}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for setup, horizons in report["setups"].items():
        print("\n", setup)
        for name, result in horizons[str(args.max_horizon_minutes)].items():
            print(name, {key: value for key, value in result.items() if key != "trades"})


if __name__ == "__main__":
    main()
