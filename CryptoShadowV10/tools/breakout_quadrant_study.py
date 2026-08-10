from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median

from structure_oi_lsr_backtest import FIVE_MINUTES_MS, load_bars, pct_change


def summarize(values: list[float]) -> dict[str, float | int]:
    return {
        "events": len(values),
        "mean_net_pct": round(mean(values), 4) if values else 0.0,
        "median_net_pct": round(median(values), 4) if values else 0.0,
        "positive_pct": round(sum(value > 0 for value in values) / len(values) * 100.0, 2) if values else 0.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Estudo de quadrantes OI/LSR apos rompimentos sustentados.")
    parser.add_argument("database", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("--cost-pct", type=float, default=0.14)
    parser.add_argument("--lookback", type=int, default=24)
    parser.add_argument("--cooldown-bars", type=int, default=12)
    args = parser.parse_args()

    connection = sqlite3.connect(args.database)
    symbols = [row[0] for row in connection.execute(
        "SELECT symbol FROM research_klines_5m GROUP BY symbol HAVING COUNT(*)>=1000 ORDER BY symbol"
    )]
    horizons = {"15m": 3, "30m": 6, "60m": 12, "180m": 36}
    groups: dict[tuple[str, str, str], dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    by_symbol: dict[tuple[tuple[str, str, str], str], dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    by_week: dict[tuple[tuple[str, str, str], int], dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))

    for symbol in symbols:
        bars = load_bars(connection, symbol)
        true_ranges = []
        for i, bar in enumerate(bars):
            previous = bars[i - 1].close if i else bar.close
            true_ranges.append(max(bar.high - bar.low, abs(bar.high - previous), abs(bar.low - previous)))
        last_event = -args.cooldown_bars
        for i in range(args.lookback + 2, len(bars) - max(horizons.values())):
            if i - last_event < args.cooldown_bars:
                continue
            base = bars[i - args.lookback - 1:i - 1]
            atr = mean(true_ranges[i - 14:i])
            if not base or atr <= 0 or (max(b.high for b in base) - min(b.low for b in base)) / atr > 6.0:
                continue
            high, low = max(b.high for b in base), min(b.low for b in base)
            penetration = 0.10 * atr
            prior, current = bars[i - 1], bars[i]
            if prior.close > high + penetration and current.close > high:
                side, sign = "LONG", 1.0
            elif prior.close < low - penetration and current.close < low:
                side, sign = "SHORT", -1.0
            else:
                continue
            oi_delta = pct_change(current.oi, bars[i - 3].oi)
            lsr_delta = pct_change(current.lsr, bars[i - 3].lsr)
            oi_state = "OI_UP" if oi_delta >= 0 else "OI_DOWN"
            lsr_state = "LSR_UP" if lsr_delta >= 0 else "LSR_DOWN"
            entry = bars[i + 1].open
            group_key = (side, oi_state, lsr_state)
            week_ms = (current.timestamp_ms // (7 * 86_400_000)) * (7 * 86_400_000)
            for label, offset in horizons.items():
                raw = sign * (bars[i + offset].close / entry - 1.0) * 100.0
                value = raw - args.cost_pct
                groups[group_key][label].append(value)
                by_symbol[(group_key, symbol)][label].append(value)
                by_week[(group_key, week_ms)][label].append(value)
            last_event = i
    connection.close()

    report = {
        "symbols": symbols,
        "assumptions": {
            "entry": "next 5m candle open", "round_trip_cost_pct": args.cost_pct,
            "range_lookback_bars": args.lookback, "cooldown_bars": args.cooldown_bars,
            "note": "diagnostic event study; not a tradable backtest",
        },
        "groups": {}, "by_symbol": {}, "by_week": {},
    }
    for key, horizon_values in sorted(groups.items()):
        label = "/".join(key)
        report["groups"][label] = {horizon: summarize(values) for horizon, values in horizon_values.items()}
    for (key, symbol), horizon_values in sorted(by_symbol.items()):
        label = "/".join(key)
        report["by_symbol"].setdefault(label, {})[symbol] = {
            horizon: summarize(values) for horizon, values in horizon_values.items()
        }
    for (key, week_ms), horizon_values in sorted(by_week.items()):
        label = "/".join(key)
        week = datetime.fromtimestamp(week_ms / 1000, timezone.utc).date().isoformat()
        report["by_week"].setdefault(label, {})[week] = {
            horizon: summarize(values) for horizon, values in horizon_values.items()
        }
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
