from __future__ import annotations

import argparse
import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean, median

from structure_oi_lsr_backtest import Bar, load_bars, pct_change


@dataclass
class Result:
    model: str
    symbol: str
    direction: str
    signal_ms: int
    entry_ms: int
    exit_ms: int
    entry: float
    stop: float
    target: float
    exit: float
    reason: str
    net_r: float
    net_pct: float
    split: str


def rolling_rsi(closes: list[float], end: int, period: int = 14) -> float:
    sample = closes[max(0, end - period): end + 1]
    if len(sample) < period + 1:
        return 50.0
    gains = [max(sample[i] - sample[i - 1], 0.0) for i in range(1, len(sample))]
    losses = [max(sample[i - 1] - sample[i], 0.0) for i in range(1, len(sample))]
    avg_gain, avg_loss = mean(gains), mean(losses)
    if avg_loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def true_ranges(bars: list[Bar]) -> list[float]:
    return [
        max(bar.high - bar.low, abs(bar.high - (bars[i - 1].close if i else bar.close)),
            abs(bar.low - (bars[i - 1].close if i else bar.close)))
        for i, bar in enumerate(bars)
    ]


def summarize(rows: list[Result]) -> dict:
    if not rows:
        return {"trades": 0}
    wins = [row.net_r for row in rows if row.net_r > 0]
    losses = [-row.net_r for row in rows if row.net_r < 0]
    equity = peak = drawdown = 0.0
    for row in sorted(rows, key=lambda item: item.entry_ms):
        equity += row.net_r
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {
        "trades": len(rows),
        "win_rate_pct": round(100 * len(wins) / len(rows), 2),
        "mean_net_r": round(mean(row.net_r for row in rows), 4),
        "median_net_r": round(median(row.net_r for row in rows), 4),
        "total_net_r": round(sum(row.net_r for row in rows), 3),
        "profit_factor": round(sum(wins) / sum(losses), 3) if losses else math.inf,
        "max_drawdown_r": round(drawdown, 3),
        "mean_net_pct": round(mean(row.net_pct for row in rows), 4),
    }


def simulate_symbol(
    model: str, symbol: str, bars: list[Bar], btc: dict[int, Bar], *,
    cost_pct: float, target_r: float, max_hold: int,
) -> list[Result]:
    closes = [bar.close for bar in bars]
    trs = true_ranges(bars)
    split_ms = bars[int(len(bars) * 0.60)].timestamp_ms
    rows: list[Result] = []
    i = 48
    while i + max_hold + 8 < len(bars):
        current = bars[i]
        base = bars[i - 18:i - 3]
        atr = mean(trs[i - 14:i])
        if atr <= 0 or len(base) < 15:
            i += 1
            continue
        base_high, base_low = max(row.high for row in base), min(row.low for row in base)
        base_width_atr = (base_high - base_low) / atr
        recent_ranges = mean(trs[i - 9:i - 3])
        prior_ranges = mean(trs[i - 30:i - 9])
        compression = recent_ranges / prior_ranges if prior_ranges else 99.0
        if base_width_atr > 5.0 or compression > 1.05:
            i += 1
            continue

        volume_baseline = mean(row.quote_volume for row in bars[i - 24:i - 3])
        volume_ratio = mean(row.quote_volume for row in bars[i - 2:i + 1]) / max(volume_baseline, 1e-12)
        trades_baseline = mean(row.trades for row in bars[i - 24:i - 3])
        trades_ratio = mean(row.trades for row in bars[i - 2:i + 1]) / max(trades_baseline, 1e-12)
        impulse = pct_change(current.close, bars[i - 3].close)
        oi_delta = pct_change(current.oi, bars[i - 3].oi)
        lsr_delta = pct_change(current.lsr, bars[i - 3].lsr)
        btc_now, btc_old = btc.get(current.timestamp_ms), btc.get(bars[i - 12].timestamp_ms)
        btc_1h = pct_change(btc_now.close, btc_old.close) if btc_now and btc_old else 0.0
        relative_1h = pct_change(current.close, bars[i - 12].close) - btc_1h
        rsi = rolling_rsi(closes, i)
        long_break = (
            current.close > base_high + 0.05 * atr and impulse >= 0.35 and
            oi_delta >= 0.05 and lsr_delta <= -0.20 and current.taker_ratio >= 1.02 and
            volume_ratio >= 1.10 and trades_ratio >= 1.10 and relative_1h > 0 and 58 <= rsi <= 88
        )
        if model == "SHORT_LEGACY":
            short_flow = oi_delta <= -0.05 and lsr_delta >= 0.20
        else:
            short_flow = oi_delta >= 0.05 and lsr_delta >= 0.20
        short_break = (
            current.close < base_low - 0.05 * atr and impulse <= -0.35 and short_flow and
            current.taker_ratio <= 0.98 and volume_ratio >= 1.10 and trades_ratio >= 1.10 and
            relative_1h < 0 and 12 <= rsi <= 42
        )
        if model == "LONG" and not long_break:
            i += 1
            continue
        if model != "LONG" and not short_break:
            i += 1
            continue
        direction = "LONG" if model == "LONG" else "SHORT"
        boundary = base_high if direction == "LONG" else base_low

        retest_index = None
        structural = None
        for j in range(i + 1, min(i + 8, len(bars) - 1)):
            row = bars[j]
            if direction == "LONG":
                touched = row.low <= boundary + 0.30 * atr
                held = row.close > boundary and row.close > row.open and row.taker_ratio >= 1.0
            else:
                touched = row.high >= boundary - 0.30 * atr
                held = row.close < boundary and row.close < row.open and row.taker_ratio <= 1.0
            if touched and held:
                retest_index = j
                structural = row.low if direction == "LONG" else row.high
                break
            invalid = row.close < base_low if direction == "LONG" else row.close > base_high
            if invalid:
                break
        if retest_index is None or structural is None:
            i += 1
            continue

        entry_index = retest_index + 1
        entry = bars[entry_index].open
        buffer = max(0.15 * atr, 0.10 / 100 * entry)
        stop = structural - buffer if direction == "LONG" else structural + buffer
        risk = abs(entry - stop)
        risk_pct = risk / entry * 100 if entry else 99.0
        if risk_pct < 0.25 or risk_pct > 2.0:
            i = entry_index
            continue
        target = entry + target_r * risk if direction == "LONG" else entry - target_r * risk
        exit_index = min(entry_index + max_hold, len(bars) - 1)
        exit_price, reason = bars[exit_index].close, "TIME"
        for j in range(entry_index, exit_index + 1):
            row = bars[j]
            stop_hit = row.low <= stop if direction == "LONG" else row.high >= stop
            target_hit = row.high >= target if direction == "LONG" else row.low <= target
            if stop_hit:
                exit_index, exit_price, reason = j, stop, "STOP"
                break
            if target_hit:
                exit_index, exit_price, reason = j, target, "TARGET"
                break
        gross = (exit_price - entry if direction == "LONG" else entry - exit_price)
        net_r = gross / risk - (entry * cost_pct / 100) / risk
        net_pct = gross / entry * 100 - cost_pct
        rows.append(Result(
            model, symbol, direction, current.timestamp_ms, bars[entry_index].timestamp_ms,
            bars[exit_index].timestamp_ms, entry, stop, target, exit_price, reason,
            net_r, net_pct, "VALIDATION" if current.timestamp_ms >= split_ms else "DEVELOPMENT",
        ))
        i = exit_index + 1
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("--cost-pct", type=float, default=0.14)
    parser.add_argument("--target-r", type=float, default=3.0)
    parser.add_argument("--max-hold", type=int, default=72)
    args = parser.parse_args()
    connection = sqlite3.connect(args.database)
    symbols = [row[0] for row in connection.execute(
        "SELECT symbol FROM research_klines_5m GROUP BY symbol HAVING COUNT(*)>=1000 ORDER BY symbol"
    )]
    btc_bars = load_bars(connection, "BTCUSDT")
    btc = {bar.timestamp_ms: bar for bar in btc_bars}
    results = []
    for symbol in symbols:
        bars = load_bars(connection, symbol)
        for model in ("LONG", "SHORT_LEGACY", "SHORT_BUILD"):
            results.extend(simulate_symbol(
                model, symbol, bars, btc, cost_pct=args.cost_pct,
                target_r=args.target_r, max_hold=args.max_hold,
            ))
    connection.close()
    report = {
        "assumptions": {
            "entry": "next 5m open after breakout retest/hold",
            "stop": "retest structure plus ATR buffer",
            "same_bar": "stop first", "cost_pct": args.cost_pct,
            "target_r": args.target_r, "max_hold_bars": args.max_hold,
            "development_validation": "chronological 60/40 per symbol",
        },
        "symbols": symbols,
        "results": {},
        "trades": [asdict(row) for row in results],
    }
    for model in ("LONG", "SHORT_LEGACY", "SHORT_BUILD"):
        report["results"][model] = {}
        for split in ("DEVELOPMENT", "VALIDATION", "ALL"):
            subset = [row for row in results if row.model == model and (split == "ALL" or row.split == split)]
            report["results"][model][split] = summarize(subset)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["results"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
