from __future__ import annotations

import argparse
import json
import sqlite3
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from tools.native_tp_runner_replay import SAO_PAULO, directional, get_1m, logical


STOP_VARIANTS = {
    "DUMP_EXHAUSTION_RECLAIM_V1": [1.50, 2.00, 2.50],
    "DUMP_REVERSAL_LONG": [1.50, 2.00, 3.00, 4.00],
    "DUMP_CONTINUATION_SHORT": [1.20, 1.50, 2.00, 2.50],
    "BORDER_BREAKOUT_RETEST_LONG": [0.65, 0.80, 1.00],
    "BORDER_BREAKOUT_RETEST_SHORT": [0.65, 0.80, 1.00],
    "FAILED_BREAKOUT_REVERSAL_LONG": [0.55, 0.80, 1.00, 1.20],
    "FAILED_BREAKOUT_REVERSAL_SHORT": [0.55, 0.80, 1.00, 1.20],
}


def load(database: Path, start_ms: int) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    db = sqlite3.connect(database)
    db.row_factory = sqlite3.Row
    executions = db.execute(
        "SELECT * FROM executions WHERE ok=1 AND timestamp_ms>=? ORDER BY timestamp_ms", (start_ms,),
    ).fetchall()
    results = {
        int(row["execution_id"]): dict(row)
        for row in db.execute("SELECT * FROM execution_results WHERE closed_at_ms>=?", (start_ms,))
    }
    db.close()
    signals = []
    for row in executions:
        signal, execution = json.loads(row["signal_json"] or "{}"), json.loads(row["result_json"] or "{}")
        setup, direction = str(signal.get("setup") or "UNKNOWN"), str(row["direction"])
        entry, stop, target = (float(signal.get(key) or 0.0) for key in (
            "entry_price", "stop_price", "target_price",
        ))
        if min(entry, stop, target) <= 0:
            continue
        evidence, market = signal.get("evidence") or {}, (signal.get("evidence") or {}).get("entry") or {}
        signals.append({
            "execution_id": int(row["id"]), "timestamp_ms": int(row["timestamp_ms"]),
            "symbol": str(row["symbol"]), "direction": direction, "setup": setup,
            "entry": entry, "stop": stop, "target": target,
            "stop_pct": abs(directional(direction, entry, stop)),
            "target_pct": directional(direction, entry, target),
            "fill_price": float(execution.get("fill_price") or 0.0),
            "testnet_stop": float(execution.get("initial_stop_price") or 0.0),
            "testnet_target": float(execution.get("initial_target_price") or 0.0),
            "entry_basis_pct": (
                (float(execution.get("fill_price")) / entry - 1.0) * 100.0
                if execution.get("fill_price") and entry else 0.0
            ),
            "entry_atr_pct": float(market.get("atr14_pct") or 0.0),
            "entry_price15_pct": float(market.get("price_change_15m_pct") or 0.0),
            "entry_oi15_pct": float(market.get("oi_change_15m_pct") or 0.0),
            "entry_taker": float(market.get("taker_buy_sell_ratio") or 0.0),
            "entry_adx": float(market.get("adx14") or 0.0),
            "entry_quality": evidence.get("entry_quality_score"),
            "rebound_from_low_pct": evidence.get("rebound_from_dump_low_pct"),
        })
    return signals, results


def prices(signal: dict[str, Any], stop_pct: float) -> tuple[float, float]:
    if signal["direction"] == "LONG":
        return signal["entry"] * (1 - stop_pct / 100.0), signal["target"]
    return signal["entry"] * (1 + stop_pct / 100.0), signal["target"]


def hit(direction: str, high: float, low: float, price: float, kind: str) -> bool:
    if kind == "STOP":
        return low <= price if direction == "LONG" else high >= price
    return high >= price if direction == "LONG" else low <= price


def replay(signal: dict[str, Any], rows: list[list[Any]], stop_pct: float) -> dict[str, Any]:
    stop, target = prices(signal, stop_pct)
    mfe, mae = 0.0, 0.0
    for index, row in enumerate(rows):
        high, low = float(row[2]), float(row[3])
        favorable, adverse = (high, low) if signal["direction"] == "LONG" else (low, high)
        mfe = max(mfe, directional(signal["direction"], signal["entry"], favorable))
        mae = min(mae, directional(signal["direction"], signal["entry"], adverse))
        if hit(signal["direction"], high, low, stop, "STOP"):
            return {"outcome": "STOP", "gross_pct": -stop_pct, "mfe_pct": mfe,
                    "mae_pct": mae, "hit_index": index, "hit_ms": int(row[0])}
        if hit(signal["direction"], high, low, target, "TARGET"):
            return {"outcome": "TARGET", "gross_pct": signal["target_pct"], "mfe_pct": mfe,
                    "mae_pct": mae, "hit_index": index, "hit_ms": int(row[0])}
    close = float(rows[-1][4])
    return {"outcome": "OPEN", "gross_pct": directional(signal["direction"], signal["entry"], close),
            "mfe_pct": mfe, "mae_pct": mae, "hit_index": None, "hit_ms": None}


def after_stop(signal: dict[str, Any], rows: list[list[Any]], native: dict[str, Any]) -> dict[str, Any] | None:
    if native["outcome"] != "STOP":
        return None
    later = rows[int(native["hit_index"]) + 1:]
    if not later:
        return {"target_later": False, "minutes_to_target": None, "later_mfe_pct": native["mfe_pct"]}
    peak, target_ms = native["mfe_pct"], None
    for row in later:
        high, low = float(row[2]), float(row[3])
        favorable = high if signal["direction"] == "LONG" else low
        peak = max(peak, directional(signal["direction"], signal["entry"], favorable))
        if target_ms is None and hit(signal["direction"], high, low, signal["target"], "TARGET"):
            target_ms = int(row[0])
    return {
        "target_later": target_ms is not None,
        "minutes_to_target": round((target_ms - int(native["hit_ms"])) / 60_000.0, 1) if target_ms else None,
        "later_mfe_pct": peak,
    }


def perf(values: list[float]) -> dict[str, Any]:
    wins, losses = [v for v in values if v > 0], [v for v in values if v < 0]
    return {
        "n": len(values), "net_sum": round(sum(values), 4),
        "mean": round(sum(values) / len(values), 4) if values else 0.0,
        "win_rate_pct": round(len(wins) / len(values) * 100.0, 2) if values else 0.0,
        "profit_factor": round(sum(wins) / -sum(losses), 3) if losses else None,
    }


def actual_summary(signals: list[dict[str, Any]], results: dict[int, dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for signal in signals:
        if signal["execution_id"] in results:
            grouped[signal["setup"]].append((signal, results[signal["execution_id"]]))
    summary = {}
    for setup, rows in grouped.items():
        pnls = [float(row[1]["net_pnl"]) for row in rows]
        pct = [float(row[1]["net_pct"]) for row in rows]
        rs = [float(row[1]["result_r"]) for row in rows]
        commissions = [float(row[1]["commission"]) for row in rows]
        reasons = defaultdict(int)
        for _, row in rows:
            reasons[str(row["exit_reason"])] += 1
        base = perf(pnls)
        summary[setup] = {
            "closed": len(rows), "net_pnl_usdt": base["net_sum"],
            "net_pct_sum": round(sum(pct), 4), "mean_r": round(sum(rs) / len(rs), 3),
            "win_rate_pct": base["win_rate_pct"], "profit_factor": base["profit_factor"],
            "commission_usdt": round(sum(commissions), 4), "exit_reasons": dict(reasons),
            "mean_abs_entry_basis_pct": round(sum(abs(row[0]["entry_basis_pct"]) for row in rows) / len(rows), 4),
        }
    return summary


def metric_means(rows: list[dict[str, Any]]) -> dict[str, Any]:
    keys = ["entry_price15_pct", "entry_oi15_pct", "entry_taker", "entry_atr_pct", "entry_adx"]
    result = {}
    for key in keys:
        values = [float(row[key]) for row in rows if row.get(key) is not None]
        result[key] = round(sum(values) / len(values), 4) if values else None
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--start", required=True)
    parser.add_argument("--cost-pct", type=float, default=0.14)
    args = parser.parse_args()
    start_ms = int(datetime.fromisoformat(args.start).timestamp() * 1000)
    now_ms, session = int(time.time() * 1000), requests.Session()
    signals, results = load(args.database, start_ms)
    trials = []
    for signal in signals:
        begin = ((signal["timestamp_ms"] // 60_000) + 1) * 60_000
        rows = get_1m(session, signal["symbol"], begin, now_ms)
        if not rows:
            continue
        native = replay(signal, rows, signal["stop_pct"])
        logical_result = logical(rows, signal)
        variants = {
            str(stop): replay(signal, rows, stop)
            for stop in STOP_VARIANTS.get(signal["setup"], [signal["stop_pct"]])
        }
        trials.append({**signal, "native": native, "logical": logical_result,
                       "post_stop": after_stop(signal, rows, native), "stop_variants": variants})
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in trials:
        grouped[row["setup"]].append(row)
    by_strategy = {}
    for setup, rows in sorted(grouped.items()):
        fixed_closed = [row for row in rows if row["native"]["outcome"] != "OPEN"]
        logical_closed = [row for row in rows if row["logical"]["outcome"] not in {"OPEN", "OPEN_ARMED"}]
        stopped = [row for row in rows if row["native"]["outcome"] == "STOP"]
        winners = [row for row in rows if row["native"]["outcome"] == "TARGET"]
        sensitivity = {}
        for stop in STOP_VARIANTS.get(setup, []):
            selected = [row["stop_variants"][str(stop)] for row in rows]
            closed = [row for row in selected if row["outcome"] != "OPEN"]
            sensitivity[str(stop)] = {
                **perf([row["gross_pct"] - args.cost_pct for row in closed]),
                "closed": len(closed), "open": len(selected) - len(closed),
                "targets": sum(row["outcome"] == "TARGET" for row in selected),
                "stops": sum(row["outcome"] == "STOP" for row in selected),
            }
        by_strategy[setup] = {
            "signals": len(rows),
            "native_target_pct_values": sorted({round(row["target_pct"], 6) for row in rows}),
            "fixed_public": {**perf([row["native"]["gross_pct"] - args.cost_pct for row in fixed_closed]),
                             "closed": len(fixed_closed), "open": len(rows) - len(fixed_closed)},
            "logical_public": {**perf([row["logical"]["gross_pct"] - args.cost_pct for row in logical_closed]),
                               "closed": len(logical_closed), "open": len(rows) - len(logical_closed)},
            "post_stop": {
                "stops": len(stopped),
                "target_reached_later": sum(bool((row.get("post_stop") or {}).get("target_later")) for row in stopped),
                "near_target_before_stop": sum(row["native"]["mfe_pct"] >= row["target_pct"] * 0.80 for row in stopped),
                "mean_mfe_before_stop_pct": round(sum(row["native"]["mfe_pct"] for row in stopped) / len(stopped), 4) if stopped else 0.0,
            },
            "entry_metrics_targets": metric_means(winners),
            "entry_metrics_stops": metric_means(stopped),
            "stop_sensitivity": sensitivity,
        }
    actual = actual_summary(signals, results)
    report = {
        "generated_at": datetime.now(SAO_PAULO).isoformat(), "sample_start": args.start,
        "assumptions": {
            "public_venue": "Binance Futures production 1m", "round_trip_cost_pct": args.cost_pct,
            "intrabar": "stop first; logical uses previous trail before new peak",
            "warning": "Testnet actual and production-public replay are separate markets",
        },
        "counts": {"accepted_signals": len(signals), "public_replayed": len(trials),
                   "testnet_closed": len(results), "pending_executions": len(signals) - len(results)},
        "actual_testnet": actual, "by_strategy": by_strategy,
        "trades": trials,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("counts", "actual_testnet", "by_strategy")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
