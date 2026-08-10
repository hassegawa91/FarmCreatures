from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean, median
from typing import Any


FIVE_MINUTES_MS = 300_000


@dataclass
class Bar:
    timestamp_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    trades: int
    taker_buy_quote: float
    oi: float
    oi_value: float
    lsr: float
    taker_ratio: float


@dataclass
class Trade:
    model: str
    symbol: str
    side: str
    signal_ms: int
    entry_ms: int
    exit_ms: int
    entry: float
    stop: float
    target: float
    exit: float
    exit_reason: str
    gross_r: float
    net_r: float
    net_pct: float
    mfe_r: float
    mae_r: float
    bars_held: int
    split: str


def pct_change(current: float, previous: float) -> float:
    return (current / previous - 1.0) * 100.0 if previous else 0.0


def load_bars(connection: sqlite3.Connection, symbol: str) -> list[Bar]:
    rows = connection.execute(
        """SELECT k.timestamp_ms,k.open,k.high,k.low,k.close,k.base_volume,k.quote_volume,
                  k.trades,k.taker_buy_quote,o.open_interest,o.open_interest_value,
                  l.long_short_ratio,t.buy_sell_ratio
           FROM research_klines_5m k
           JOIN research_oi_5m o
             ON o.symbol=k.symbol AND o.timestamp_ms=k.timestamp_ms+?
           JOIN research_global_lsr_5m l
             ON l.symbol=k.symbol AND l.timestamp_ms=k.timestamp_ms+?
           JOIN research_taker_5m t
             ON t.symbol=k.symbol AND t.timestamp_ms=k.timestamp_ms
           WHERE k.symbol=? ORDER BY k.timestamp_ms""",
        (FIVE_MINUTES_MS, FIVE_MINUTES_MS, symbol),
    ).fetchall()
    return [Bar(*row) for row in rows]


def resample_bars(bars: list[Bar], minutes: int) -> list[Bar]:
    multiple = minutes // 5
    if multiple == 1:
        return bars
    grouped: list[Bar] = []
    bucket_ms = minutes * 60_000
    current: list[Bar] = []
    current_key: int | None = None
    for bar in bars:
        key = bar.timestamp_ms // bucket_ms
        if current and key != current_key:
            if len(current) == multiple:
                buy_quote = sum(item.taker_buy_quote for item in current)
                sell_quote = max(sum(item.quote_volume for item in current) - buy_quote, 1e-12)
                grouped.append(Bar(
                    current[0].timestamp_ms, current[0].open,
                    max(item.high for item in current), min(item.low for item in current), current[-1].close,
                    sum(item.volume for item in current), sum(item.quote_volume for item in current),
                    sum(item.trades for item in current), buy_quote,
                    current[-1].oi, current[-1].oi_value, current[-1].lsr, buy_quote / sell_quote,
                ))
            current = []
        current_key = key
        current.append(bar)
    if len(current) == multiple:
        buy_quote = sum(item.taker_buy_quote for item in current)
        sell_quote = max(sum(item.quote_volume for item in current) - buy_quote, 1e-12)
        grouped.append(Bar(
            current[0].timestamp_ms, current[0].open,
            max(item.high for item in current), min(item.low for item in current), current[-1].close,
            sum(item.volume for item in current), sum(item.quote_volume for item in current),
            sum(item.trades for item in current), buy_quote,
            current[-1].oi, current[-1].oi_value, current[-1].lsr, buy_quote / sell_quote,
        ))
    return grouped


def rolling_mean(values: list[float], start: int, end: int) -> float:
    window = values[max(0, start):end]
    return mean(window) if window else 0.0


def signal_for(
    model: str, bars: list[Bar], i: int, atr: float, lookback: int, metric_bars: int,
) -> tuple[str, float, float] | None:
    # The two most recent closed bars are reserved for breakout/sustain or failure/reclaim.
    base = bars[i - lookback - 1:i - 1]
    if len(base) < lookback:
        return None
    prior_high = max(bar.high for bar in base)
    prior_low = min(bar.low for bar in base)
    prior = bars[i - 1]
    current = bars[i]
    if atr <= 0:
        return None

    range_atr = (prior_high - prior_low) / atr
    if range_atr > 6.0:
        return None

    oi_15m = pct_change(current.oi, bars[i - metric_bars].oi)
    lsr_15m = pct_change(current.lsr, bars[i - metric_bars].lsr)
    volume_ratio = current.quote_volume / max(mean(bar.quote_volume for bar in bars[i - 20:i]), 1e-12)
    trades_ratio = current.trades / max(mean(bar.trades for bar in bars[i - 20:i]), 1e-12)
    penetration = 0.10 * atr

    long_break = prior.close > prior_high + penetration and current.close > prior_high
    short_break = prior.close < prior_low - penetration and current.close < prior_low
    if model in {"CONTINUATION_CORE", "CONTINUATION_FLOW"}:
        if long_break and oi_15m >= 0.05 and lsr_15m <= -0.30:
            if model == "CONTINUATION_FLOW" and not (
                volume_ratio >= 1.15 and trades_ratio >= 1.15 and current.taker_ratio >= 1.03
            ):
                return None
            stop = min(prior_high - 0.25 * atr, min(prior.low, current.low) - 0.10 * atr)
            return "LONG", stop, prior_high
        if short_break and oi_15m <= -0.05 and lsr_15m >= 0.30:
            if model == "CONTINUATION_FLOW" and not (
                volume_ratio >= 1.15 and trades_ratio >= 1.15 and current.taker_ratio <= 0.97
            ):
                return None
            stop = max(prior_low + 0.25 * atr, max(prior.high, current.high) + 0.10 * atr)
            return "SHORT", stop, prior_low
        return None

    if model == "FAILED_BREAK_REVERSAL":
        failed_high = prior.high > prior_high + penetration and current.close < prior_high
        failed_low = prior.low < prior_low - penetration and current.close > prior_low
        if failed_high and current.close < prior.close and current.taker_ratio <= 0.98:
            return "SHORT", max(prior.high, current.high) + 0.20 * atr, prior_high
        if failed_low and current.close > prior.close and current.taker_ratio >= 1.02:
            return "LONG", min(prior.low, current.low) - 0.20 * atr, prior_low
    return None


def simulate(
    model: str, symbol: str, bars: list[Bar], *, fee_pct: float, slippage_pct: float,
    lookback: int = 24, target_r: float = 2.0, max_hold: int = 36, interval_ms: int = FIVE_MINUTES_MS,
) -> list[Trade]:
    trades: list[Trade] = []
    true_ranges: list[float] = []
    for i, bar in enumerate(bars):
        previous_close = bars[i - 1].close if i else bar.close
        true_ranges.append(max(bar.high - bar.low, abs(bar.high - previous_close), abs(bar.low - previous_close)))

    i = max(lookback + 2, 20)
    metric_bars = max(1, int(round(15 * 60_000 / interval_ms)))
    split_ms = bars[int(len(bars) * 0.60)].timestamp_ms if bars else 0
    while i + 1 < len(bars):
        atr = rolling_mean(true_ranges, i - 14, i)
        signal = signal_for(model, bars, i, atr, lookback, metric_bars)
        if not signal:
            i += 1
            continue
        side, proposed_stop, _boundary = signal
        entry_index = i + 1
        entry = bars[entry_index].open
        if side == "LONG":
            stop = min(proposed_stop, entry - 0.35 * atr)
            risk = entry - stop
            target = entry + target_r * risk
        else:
            stop = max(proposed_stop, entry + 0.35 * atr)
            risk = stop - entry
            target = entry - target_r * risk
        if risk <= 0 or risk / entry > 0.05:
            i += 1
            continue

        exit_index = min(entry_index + max_hold, len(bars) - 1)
        exit_price = bars[exit_index].close
        reason = "TIME"
        mfe = mae = 0.0
        for j in range(entry_index, exit_index + 1):
            bar = bars[j]
            if side == "LONG":
                mfe = max(mfe, (bar.high - entry) / risk)
                mae = max(mae, (entry - bar.low) / risk)
                if bar.low <= stop:
                    exit_index, exit_price, reason = j, stop, "STOP"
                    break
                if bar.high >= target:
                    exit_index, exit_price, reason = j, target, "TARGET"
                    break
            else:
                mfe = max(mfe, (entry - bar.low) / risk)
                mae = max(mae, (bar.high - entry) / risk)
                if bar.high >= stop:
                    exit_index, exit_price, reason = j, stop, "STOP"
                    break
                if bar.low <= target:
                    exit_index, exit_price, reason = j, target, "TARGET"
                    break

        gross_r = ((exit_price - entry) if side == "LONG" else (entry - exit_price)) / risk
        cost_pct = fee_pct + 2.0 * slippage_pct
        cost_r = (entry * cost_pct / 100.0) / risk
        net_r = gross_r - cost_r
        net_pct = ((exit_price - entry) / entry if side == "LONG" else (entry - exit_price) / entry) * 100.0 - cost_pct
        trades.append(Trade(
            model, symbol, side, bars[i].timestamp_ms + interval_ms,
            bars[entry_index].timestamp_ms, bars[exit_index].timestamp_ms,
            entry, stop, target, exit_price, reason, gross_r, net_r, net_pct,
            mfe, mae, exit_index - entry_index + 1,
            "VALIDATION" if bars[i].timestamp_ms >= split_ms else "DEVELOPMENT",
        ))
        i = exit_index + 1
    return trades


def summarize(trades: list[Trade]) -> dict[str, Any]:
    if not trades:
        return {"trades": 0}
    wins = [trade for trade in trades if trade.net_r > 0]
    gains = sum(trade.net_r for trade in trades if trade.net_r > 0)
    losses = -sum(trade.net_r for trade in trades if trade.net_r < 0)
    equity = peak = max_drawdown = 0.0
    for trade in trades:
        equity += trade.net_r
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    return {
        "trades": len(trades),
        "win_rate_pct": round(len(wins) / len(trades) * 100.0, 2),
        "mean_net_r": round(mean(trade.net_r for trade in trades), 4),
        "median_net_r": round(median(trade.net_r for trade in trades), 4),
        "total_net_r": round(sum(trade.net_r for trade in trades), 3),
        "profit_factor": round(gains / losses, 3) if losses else math.inf,
        "max_drawdown_r": round(max_drawdown, 3),
        "mean_net_pct": round(mean(trade.net_pct for trade in trades), 4),
        "mean_mfe_r": round(mean(trade.mfe_r for trade in trades), 3),
        "mean_mae_r": round(mean(trade.mae_r for trade in trades), 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Triagem sem lookahead das hipoteses Telegram.")
    parser.add_argument("database", type=Path)
    parser.add_argument("trades_csv", type=Path)
    parser.add_argument("report_json", type=Path)
    parser.add_argument("--fee-pct", type=float, default=0.10, help="Custo total de taxas por round trip")
    parser.add_argument("--slippage-pct", type=float, default=0.02, help="Slippage por lado")
    parser.add_argument("--target-r", type=float, default=2.0)
    parser.add_argument("--max-hold", type=int, default=36)
    parser.add_argument("--bar-minutes", type=int, choices=(5, 15, 30, 60), default=5)
    args = parser.parse_args()

    connection = sqlite3.connect(args.database)
    symbols = [row[0] for row in connection.execute(
        "SELECT symbol FROM research_klines_5m GROUP BY symbol HAVING COUNT(*)>=1000 ORDER BY symbol"
    )]
    all_trades: list[Trade] = []
    for symbol in symbols:
        bars = resample_bars(load_bars(connection, symbol), args.bar_minutes)
        for model in ["CONTINUATION_CORE", "CONTINUATION_FLOW", "FAILED_BREAK_REVERSAL"]:
            all_trades.extend(simulate(
                model, symbol, bars, fee_pct=args.fee_pct, slippage_pct=args.slippage_pct,
                target_r=args.target_r, max_hold=args.max_hold, interval_ms=args.bar_minutes * 60_000,
            ))
    connection.close()

    args.trades_csv.parent.mkdir(parents=True, exist_ok=True)
    with args.trades_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(all_trades[0]).keys()) if all_trades else Trade.__annotations__.keys())
        writer.writeheader()
        writer.writerows(asdict(trade) for trade in all_trades)

    report: dict[str, Any] = {"symbols": symbols, "assumptions": {
        "fee_round_trip_pct": args.fee_pct, "slippage_each_side_pct": args.slippage_pct,
        "entry": "next candle open", "same_bar_stop_target": "stop first (pessimistic)",
        "development_fraction": 0.60, "validation_fraction": 0.40,
        "target_r": args.target_r, "max_hold_bars": args.max_hold,
        "bar_minutes": args.bar_minutes,
    }, "results": {}}
    for model in ["CONTINUATION_CORE", "CONTINUATION_FLOW", "FAILED_BREAK_REVERSAL"]:
        report["results"][model] = {}
        for split in ["DEVELOPMENT", "VALIDATION", "ALL"]:
            subset = [trade for trade in all_trades if trade.model == model and (split == "ALL" or trade.split == split)]
            report["results"][model][split] = summarize(subset)
    args.report_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
