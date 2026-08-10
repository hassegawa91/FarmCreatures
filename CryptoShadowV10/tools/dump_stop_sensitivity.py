from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import requests


def directional(direction: str, entry: float, price: float) -> float:
    move = (price - entry) / entry * 100.0
    return move if direction == "LONG" else -move


def load_signals(database: Path) -> list[dict[str, Any]]:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "SELECT timestamp_ms,symbol,direction,signal_json FROM executions "
        "WHERE ok=1 ORDER BY timestamp_ms"
    ).fetchall()
    connection.close()
    result = []
    for row in rows:
        signal = json.loads(row["signal_json"] or "{}")
        if signal.get("setup") != "DUMP_EXHAUSTION_RECLAIM_V1":
            continue
        result.append({
            "timestamp_ms": int(row["timestamp_ms"]), "symbol": str(row["symbol"]),
            "direction": str(row["direction"]), "entry": float(signal["entry_price"]),
            "entry_market": (signal.get("evidence") or {}).get("entry") or {},
        })
    return result


def klines(session: requests.Session, symbol: str, start_ms: int, end_ms: int) -> list[list[Any]]:
    response = session.get(
        "https://fapi.binance.com/fapi/v1/klines",
        params={"symbol": symbol, "interval": "5m", "startTime": start_ms, "endTime": end_ms, "limit": 1000},
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def simulate(rows: list[list[Any]], entry: float, stop_pct: float, target_pct: float) -> dict[str, Any]:
    stop = entry * (1.0 - stop_pct / 100.0)
    target = entry * (1.0 + target_pct / 100.0)
    mfe, mae = 0.0, 0.0
    for row in rows:
        high, low = float(row[2]), float(row[3])
        mfe = max(mfe, directional("LONG", entry, high))
        mae = min(mae, directional("LONG", entry, low))
        if low <= stop:
            return {"outcome": "STOP", "gross_pct": -stop_pct, "mfe_pct": mfe, "mae_pct": mae}
        if high >= target:
            return {"outcome": "TARGET", "gross_pct": target_pct, "mfe_pct": mfe, "mae_pct": mae}
    close = float(rows[-1][4])
    return {"outcome": "TIME", "gross_pct": directional("LONG", entry, close), "mfe_pct": mfe, "mae_pct": mae}


def summary(rows: list[dict[str, Any]], cost_pct: float) -> dict[str, Any]:
    nets = [row["gross_pct"] - cost_pct for row in rows]
    wins = [value for value in nets if value > 0]
    losses = [value for value in nets if value < 0]
    return {
        "n": len(rows), "wins": len(wins), "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(rows) * 100.0, 2) if rows else 0.0,
        "net_pct_sum": round(sum(nets), 4),
        "mean_net_pct": round(sum(nets) / len(rows), 4) if rows else 0.0,
        "profit_factor": round(sum(wins) / -sum(losses), 3) if losses else None,
        "outcomes": {name: sum(row["outcome"] == name for row in rows) for name in ("TARGET", "STOP", "TIME")},
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--additional-database", action="append", type=Path, default=[])
    parser.add_argument("--horizon-minutes", type=int, default=240)
    parser.add_argument("--cost-pct", type=float, default=0.10)
    args = parser.parse_args()
    signals = load_signals(args.database)
    for database in args.additional_database:
        signals.extend(load_signals(database))
    signals = list({
        (row["timestamp_ms"], row["symbol"], row["direction"]): row for row in signals
    }.values())
    now_ms = int(time.time() * 1000)
    horizons = [60, 120, args.horizon_minutes]
    stops = [1.2, 1.5, 1.8, 2.0, 2.5]
    targets = [3.0]
    session = requests.Session()
    paths: dict[tuple[int, str], list[list[Any]]] = {}
    for signal in signals:
        key = (signal["timestamp_ms"], signal["symbol"])
        paths[key] = klines(
            session, signal["symbol"], signal["timestamp_ms"],
            min(now_ms, signal["timestamp_ms"] + args.horizon_minutes * 60_000),
        )
    report: dict[str, Any] = {"assumptions": {
        "venue": "Binance Futures production public klines", "timeframe": "5m",
        "same_candle": "stop first", "cost_pct": args.cost_pct,
    }, "signals": len(signals), "grid": {}}
    for horizon in horizons:
        cutoff_age = horizon * 60_000
        eligible = [signal for signal in signals if now_ms - signal["timestamp_ms"] >= cutoff_age]
        report["grid"][str(horizon)] = {}
        for stop_pct in stops:
            for target_pct in targets:
                simulations = []
                for signal in eligible:
                    path = [
                        row for row in paths[(signal["timestamp_ms"], signal["symbol"])]
                        if int(row[0]) < signal["timestamp_ms"] + cutoff_age
                    ]
                    if not path:
                        continue
                    item = simulate(path, signal["entry"], stop_pct, target_pct)
                    simulations.append({**signal, **item})
                report["grid"][str(horizon)][f"SL{stop_pct:.1f}_TP{target_pct:.1f}"] = {
                    **summary(simulations, args.cost_pct), "trades": simulations,
                }
        dynamic_models = {
            "STRUCTURE_1.5_2.0_TP3.0": (1.5, 2.0),
            "STRUCTURE_1.5_2.2_TP3.0": (1.5, 2.2),
            "ATR065_1.5_2.2_TP3.0": (1.5, 2.2),
        }
        for name, (minimum, maximum) in dynamic_models.items():
            simulations = []
            for signal in eligible:
                market = signal["entry_market"]
                if name.startswith("STRUCTURE"):
                    lows = [float(value) for value in (
                        market.get("candle_low"), market.get("previous_candle_low"),
                    ) if float(value or 0.0) > 0]
                    structural = ((signal["entry"] - min(lows)) / signal["entry"] * 100.0 + 0.10) if lows else minimum
                    stop_pct = max(minimum, min(maximum, structural))
                else:
                    stop_pct = max(minimum, min(maximum, float(market.get("atr14_pct") or 0.0) * 0.65))
                path = [
                    row for row in paths[(signal["timestamp_ms"], signal["symbol"])]
                    if int(row[0]) < signal["timestamp_ms"] + cutoff_age
                ]
                if not path:
                    continue
                item = simulate(path, signal["entry"], stop_pct, 3.0)
                simulations.append({**signal, "tested_stop_pct": stop_pct, **item})
            report["grid"][str(horizon)][name] = {
                **summary(simulations, args.cost_pct), "trades": simulations,
            }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for horizon, grid in report["grid"].items():
        print("HORIZON", horizon)
        for name, row in grid.items():
            print(name, {key: value for key, value in row.items() if key != "trades"})


if __name__ == "__main__":
    main()
