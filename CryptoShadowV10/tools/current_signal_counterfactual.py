from __future__ import annotations

import argparse
import bisect
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any


def directional_pct(direction: str, entry: float, price: float) -> float:
    raw = (price - entry) / entry * 100.0 if entry else 0.0
    return raw if direction == "LONG" else -raw


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [row for row in rows if row.get("covered")]
    wins = [row["net_pct"] for row in valid if row["net_pct"] > 0]
    losses = [row["net_pct"] for row in valid if row["net_pct"] < 0]
    return {
        "signals": len(rows), "covered": len(valid),
        "wins": len(wins), "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(valid) * 100.0, 2) if valid else 0.0,
        "net_pct_sum": round(sum(row["net_pct"] for row in valid), 4),
        "mean_net_pct": round(sum(row["net_pct"] for row in valid) / len(valid), 4) if valid else 0.0,
        "expectancy_r": round(sum(row["result_r"] for row in valid) / len(valid), 4) if valid else 0.0,
        "profit_factor": round(sum(wins) / -sum(losses), 3) if losses else None,
        "median_mfe_r": round(sorted(row["mfe_r"] for row in valid)[len(valid) // 2], 3) if valid else None,
        "median_mae_r": round(sorted(row["mae_r"] for row in valid)[len(valid) // 2], 3) if valid else None,
    }


def study(database: Path, max_hold_minutes: int, cost_pct: float) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    observations: dict[str, list[dict[str, Any]]] = defaultdict(list)
    times: dict[str, list[int]] = defaultdict(list)
    for row in connection.execute(
        "SELECT timestamp_ms,symbol,snapshot_json,return_60m_pct FROM feature_observations ORDER BY symbol,timestamp_ms"
    ):
        snapshot = json.loads(row["snapshot_json"])
        item = {
            "timestamp_ms": int(row["timestamp_ms"]), "snapshot": snapshot,
            "return_60m_pct": row["return_60m_pct"],
        }
        observations[row["symbol"]].append(item)
        times[row["symbol"]].append(item["timestamp_ms"])

    executions: dict[tuple[int, str], dict[str, Any]] = {}
    for row in connection.execute("SELECT timestamp_ms,symbol,ok,result_json FROM executions"):
        result = json.loads(row["result_json"] or "{}")
        executions[(int(row["timestamp_ms"]), str(row["symbol"]))] = {
            "ok": bool(row["ok"]), "error": result.get("error") or result.get("reason"),
        }

    results: list[dict[str, Any]] = []
    horizon_ms = max_hold_minutes * 60_000
    events = connection.execute(
        "SELECT timestamp_ms,symbol,direction,reason,payload_json FROM events WHERE type='SIGNAL' ORDER BY id"
    ).fetchall()
    for event in events:
        signal = json.loads(event["payload_json"] or "{}")
        symbol, direction = str(event["symbol"]), str(event["direction"])
        timestamp = int(event["timestamp_ms"])
        entry = float(signal.get("entry_price") or 0.0)
        stop = float(signal.get("stop_price") or 0.0)
        target = float(signal.get("target_price") or 0.0)
        risk_pct = abs(entry - stop) / entry * 100.0 if entry else 0.0
        series, stamps = observations.get(symbol, []), times.get(symbol, [])
        start = bisect.bisect_right(stamps, timestamp)
        end = bisect.bisect_right(stamps, timestamp + horizon_ms)
        future = series[start:end]
        seen_candles: set[int] = set()
        outcome, exit_price = "", 0.0
        mfe, mae = 0.0, 0.0
        for item in future:
            snap = item["snapshot"]
            candle_ms = int(snap.get("candle_open_time_ms") or item["timestamp_ms"])
            if candle_ms in seen_candles:
                continue
            seen_candles.add(candle_ms)
            high = float(snap.get("candle_high") or snap.get("price") or entry)
            low = float(snap.get("candle_low") or snap.get("price") or entry)
            favorable = high if direction == "LONG" else low
            adverse = low if direction == "LONG" else high
            mfe = max(mfe, directional_pct(direction, entry, favorable))
            mae = min(mae, directional_pct(direction, entry, adverse))
            stop_hit = low <= stop if direction == "LONG" else high >= stop
            target_hit = high >= target if direction == "LONG" else low <= target
            if stop_hit:  # conservative when both levels occur in the same candle
                outcome, exit_price = "STOP", stop
                break
            if target_hit:
                outcome, exit_price = "TARGET", target
                break
        if not outcome:
            base_index = bisect.bisect_left(stamps, timestamp)
            base = series[base_index] if base_index < len(series) and abs(series[base_index]["timestamp_ms"] - timestamp) <= 120_000 else None
            terminal_return = base.get("return_60m_pct") if base else None
            if terminal_return is not None:
                exit_price = entry * (1.0 + float(terminal_return) / 100.0)
                outcome = "TIME_60M"
            elif future:
                last = future[-1]["snapshot"]
                exit_price = float(last.get("price") or 0.0)
                if future[-1]["timestamp_ms"] - timestamp >= horizon_ms - 120_000 and exit_price > 0:
                    outcome = "TIME_60M"
        covered = bool(outcome and entry > 0 and risk_pct > 0)
        gross = directional_pct(direction, entry, exit_price) if covered else 0.0
        net = gross - cost_pct if covered else 0.0
        execution = executions.get((timestamp, symbol), {"ok": False, "error": "execution_record_missing"})
        results.append({
            "timestamp_ms": timestamp, "symbol": symbol, "direction": direction,
            "setup": signal.get("setup") or "UNKNOWN", "signal_reason": event["reason"],
            "entry_price": entry, "stop_price": stop, "target_price": target,
            "risk_pct": risk_pct, "outcome": outcome or "UNCOVERED", "covered": covered,
            "gross_pct": gross, "net_pct": net, "result_r": net / risk_pct if covered else 0.0,
            "mfe_r": mfe / risk_pct if risk_pct else 0.0, "mae_r": mae / risk_pct if risk_pct else 0.0,
            "executed": execution["ok"], "execution_error": execution.get("error"),
        })
    connection.close()
    grouped: dict[str, Any] = {}
    for setup in sorted({row["setup"] for row in results}):
        selected = [row for row in results if row["setup"] == setup]
        grouped[setup] = {
            "ALL_SIGNALS": summarize(selected),
            "EXECUTED": summarize([row for row in selected if row["executed"]]),
            "BLOCKED": summarize([row for row in selected if not row["executed"]]),
        }
    return {
        "assumptions": {
            "entry": "signal mark price", "horizon_minutes": max_hold_minutes,
            "same_candle": "stop first", "round_trip_cost_pct": cost_pct,
            "coverage_warning": "prefiltered snapshots may be sparse after blocked signals",
        },
        "summary": summarize(results), "by_setup": grouped, "signals": results,
    }


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Contrafactual dos sinais da engine atual", "",
        "Inclui sinais executados e bloqueados. Custos e regra stop-first estao nas premissas do JSON.", "",
        "| Setup | Grupo | Sinais | Cobertos | W/L | PF | Exp. R | Net % | MFE med. R |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for setup, groups in report["by_setup"].items():
        for group, row in groups.items():
            pf = "-" if row["profit_factor"] is None else f"{row['profit_factor']:.3f}"
            lines.append(
                f"| {setup} | {group} | {row['signals']} | {row['covered']} | "
                f"{row['wins']}/{row['losses']} | {pf} | {row['expectancy_r']:.3f} | "
                f"{row['net_pct_sum']:.3f} | {row['median_mfe_r'] if row['median_mfe_r'] is not None else '-'} |"
            )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("json_output", type=Path)
    parser.add_argument("markdown_output", type=Path)
    parser.add_argument("--max-hold-minutes", type=int, default=60)
    parser.add_argument("--cost-pct", type=float, default=0.10)
    args = parser.parse_args()
    report = study(args.database, args.max_hold_minutes, args.cost_pct)
    args.json_output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    args.markdown_output.write_text(markdown(report), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
