from __future__ import annotations

import argparse
import json
import math
from collections import Counter
import sys
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.binance import MarketSnapshot, change_pct, number  # noqa: E402
from engine.config import load_config  # noqa: E402
from engine.strategy import OIBreakoutRetestStrategy  # noqa: E402


FAPI = "https://fapi.binance.com"


def get(path, params):
    response = requests.get(FAPI + path, params=params, timeout=20)
    response.raise_for_status()
    return response.json()


def load_history(symbol, limit):
    klines = get("/fapi/v1/klines", {"symbol": symbol, "interval": "5m", "limit": limit})
    oi = get("/futures/data/openInterestHist", {"symbol": symbol, "period": "5m", "limit": limit})
    lsr = get("/futures/data/globalLongShortAccountRatio", {"symbol": symbol, "period": "5m", "limit": limit})
    taker = get("/futures/data/takerlongshortRatio", {"symbol": symbol, "period": "5m", "limit": limit})
    oi_by_time = {int(row["timestamp"]): row for row in oi}
    lsr_by_time = {int(row["timestamp"]): row for row in lsr}
    taker_by_time = {int(row["timestamp"]): row for row in taker}
    return klines, oi_by_time, lsr_by_time, taker_by_time


def snapshot_at(symbol, klines, oi, lsr, taker, index, cfg):
    timestamp = int(klines[index][0])
    aligned_times = sorted(time for time in oi if time <= timestamp)
    if len(aligned_times) < 4:
        return None
    times = aligned_times[-4:]
    if any(time not in lsr or time not in taker for time in times):
        return None
    closes = [number(row[4]) for row in klines[: index + 1]]
    highs = [number(row[2]) for row in klines[: index + 1]]
    lows = [number(row[3]) for row in klines[: index + 1]]
    volumes = [number(row[7]) for row in klines[: index + 1]]
    impulse = int(cfg["impulse_lookback_bars"])
    breakout = int(cfg["breakout_lookback_bars"])
    if len(closes) < breakout + impulse + 2:
        return None
    oi_values = [number(oi[time]["sumOpenInterest"]) for time in times]
    lsr_values = [number(lsr[time]["longShortRatio"]) for time in times]
    recent = sum(volumes[-impulse:]) / impulse
    baseline = sum(volumes[-20:-impulse]) / len(volumes[-20:-impulse])
    prior = slice(-breakout - impulse, -impulse)
    return MarketSnapshot(
        symbol=symbol, timestamp_ms=timestamp, price=closes[-1],
        price_change_15m_pct=change_pct(closes[-1], closes[-1 - impulse]),
        oi_change_15m_pct=change_pct(oi_values[-1], oi_values[0]),
        global_lsr=lsr_values[-1],
        taker_buy_sell_ratio=number(taker[times[-1]]["buySellRatio"]),
        funding_rate_pct=0.0, volume_ratio=recent / baseline if baseline else 0.0,
        spread_pct=0.0, prior_high=max(highs[prior]), prior_low=min(lows[prior]),
        candle_high=highs[-1], candle_low=lows[-1],
    )


def run_symbol(symbol, limit, cfg):
    klines, oi, lsr, taker = load_history(symbol, limit)
    strategy = OIBreakoutRetestStrategy(cfg)
    fee = float(cfg["fee_pct_per_side"])
    position = None
    results = []
    events = Counter()
    for index in range(25, len(klines)):
        snap = snapshot_at(symbol, klines, oi, lsr, taker, index, cfg)
        if not snap:
            continue
        if position:
            long = position["direction"] == "LONG"
            hit_stop = snap.candle_low <= position["stop_price"] if long else snap.candle_high >= position["stop_price"]
            hit_target = snap.candle_high >= position["target_price"] if long else snap.candle_low <= position["target_price"]
            if hit_stop or hit_target:
                # Conservative when both levels occur inside one historical candle.
                exit_price = position["stop_price"] if hit_stop else position["target_price"]
                reason = "STOP" if hit_stop else "TARGET_2R"
                sign = 1 if long else -1
                gross = (exit_price - position["entry_price"]) / position["entry_price"] * 100 * sign
                net = gross - 2 * fee
                results.append({**position, "exit_reason": reason, "net_pct": net, "result_r": net / position["risk_pct"]})
                position = None
        event = strategy.observe(snap)
        if event:
            events[event.type] += 1
        if event and event.type == "SIGNAL" and not position:
            position = event.payload
    if position:
        events["OPEN_AT_END"] += 1
    return results, events


def main():
    parser = argparse.ArgumentParser(description="Replay do setup unico OI breakout + retest")
    parser.add_argument("symbols", nargs="*", default=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    parser.add_argument("--limit", type=int, default=500, choices=range(50, 501), metavar="50..500")
    args = parser.parse_args()
    cfg = load_config()["strategy"]
    all_results = []
    all_events = Counter()
    for symbol in args.symbols:
        rows, events = run_symbol(symbol.upper(), args.limit, cfg)
        all_results.extend(rows)
        all_events.update(events)
        print(f"{symbol.upper()}: trades={len(rows)} events={dict(events)}")
    wins = [row["net_pct"] for row in all_results if row["net_pct"] > 0]
    losses = [row["net_pct"] for row in all_results if row["net_pct"] < 0]
    print(json.dumps({
        "trades": len(all_results),
        "net_pct_sum": sum(row["net_pct"] for row in all_results),
        "expectancy_r": sum(row["result_r"] for row in all_results) / len(all_results) if all_results else 0,
        "win_rate_pct": len(wins) / len(all_results) * 100 if all_results else 0,
        "profit_factor": sum(wins) / -sum(losses) if losses else None,
        "events": dict(all_events),
        "caveat": "spread historico indisponivel; funding tratado como neutro; resultado nao autoriza execucao real",
    }, indent=2))


if __name__ == "__main__":
    main()
