from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median

from structure_oi_lsr_backtest import load_bars, pct_change


LOCAL = timezone(timedelta(hours=-3))
HORIZONS = (15, 30, 60, 180)


def timestamp_ms(raw: str) -> int | None:
    try:
        base = raw.split(" UTC", 1)[0]
        value = datetime.strptime(base, "%d.%m.%Y %H:%M:%S").replace(tzinfo=LOCAL)
        return int(value.timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def metric(values: list[float]) -> dict:
    return {
        "n": len(values),
        "mean": round(mean(values), 4) if values else 0,
        "median": round(median(values), 4) if values else 0,
        "positive_pct": round(100 * sum(value > 0 for value in values) / len(values), 2) if values else 0,
    }


def trade_metric(values: list[float]) -> dict:
    result = metric(values)
    gains = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    result["profit_factor"] = round(gains / losses, 3) if losses else 99.0
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("events_db", type=Path)
    parser.add_argument("market_db", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("--cost-pct", type=float, default=0.14)
    args = parser.parse_args()

    market = sqlite3.connect(args.market_db)
    symbols = {row[0] for row in market.execute("SELECT DISTINCT symbol FROM research_klines_5m")}
    bars = {}
    feature_bars = {}
    for symbol in symbols:
        feature_bars[symbol] = load_bars(market, symbol)
        bars[symbol] = market.execute(
            "SELECT timestamp_ms,open,high,low,close FROM research_klines_5m WHERE symbol=? ORDER BY timestamp_ms",
            (symbol,),
        ).fetchall()
    market.close()
    if not bars:
        raise RuntimeError("market database is empty")
    first_ms = min(rows[0][0] for rows in bars.values() if rows)
    last_ms = max(rows[-1][0] for rows in bars.values() if rows)

    events = sqlite3.connect(args.events_db)
    raw_events = []
    for row in events.execute(
        """SELECT id,source_id,timestamp_raw,event_type,symbol,direction,payload_json
           FROM external_events WHERE event_type IN ('PUMP_DUMP','LIQUIDATION') AND symbol IS NOT NULL"""
    ):
        event_ms = timestamp_ms(row[2])
        if event_ms is None or not (first_ms <= event_ms <= last_ms) or row[4] not in symbols:
            continue
        raw_events.append((*row, event_ms))
    events.close()
    raw_events.sort(key=lambda item: item[-1])

    # One independent event per continuous burst. Later alerts enrich count/size,
    # but do not become extra trades.
    clusters = []
    active = {}
    for event_id, source_id, raw, event_type, symbol, direction, payload_raw, event_ms in raw_events:
        key = (source_id, event_type, symbol, direction)
        gap = 15 * 60_000 if event_type == "PUMP_DUMP" else 5 * 60_000
        current = active.get(key)
        payload = json.loads(payload_raw)
        if current is None or event_ms - current["last_ms"] > gap:
            current = {
                "source_id": source_id, "event_type": event_type, "symbol": symbol,
                "direction": direction, "event_ms": event_ms, "last_ms": event_ms,
                "count": 0, "notional_usd": 0.0, "max_move_pct": 0.0,
            }
            clusters.append(current)
            active[key] = current
        current["last_ms"] = event_ms
        current["count"] += 1
        current["notional_usd"] += float(payload.get("notional_usd") or 0.0)
        current["max_move_pct"] = max(current["max_move_pct"], abs(float(payload.get("move_pct") or 0.0)))

    outcomes = []
    for cluster in clusters:
        rows = bars[cluster["symbol"]]
        decision_gap = 15 * 60_000 if cluster["event_type"] == "PUMP_DUMP" else 5 * 60_000
        decision_ms = cluster["last_ms"] + decision_gap
        future = [row for row in rows if row[0] > decision_ms]
        if len(future) < 36:
            continue
        entry = future[0][1]
        sign = 1 if cluster["direction"] == "LONG" else -1
        result = dict(cluster)
        result["decision_ms"] = decision_ms
        result["entry"] = entry
        history = feature_bars[cluster["symbol"]]
        feature_index = max((i for i, bar in enumerate(history) if bar.timestamp_ms <= decision_ms), default=-1)
        if feature_index >= 20:
            bar = history[feature_index]
            result.update({
                "oi_15m_pct": pct_change(bar.oi, history[feature_index - 3].oi),
                "lsr_15m_pct": pct_change(bar.lsr, history[feature_index - 3].lsr),
                "taker_ratio": bar.taker_ratio,
                "volume_ratio": bar.quote_volume / max(mean(item.quote_volume for item in history[feature_index - 20:feature_index]), 1e-12),
                "confirmation_candle": "GREEN" if bar.close > bar.open else "RED",
            })
        for horizon in HORIZONS:
            offset = horizon // 5 - 1
            if offset >= len(future):
                continue
            directional = sign * (future[offset][4] / entry - 1) * 100
            result[f"continue_{horizon}"] = directional - args.cost_pct
            result[f"fade_{horizon}"] = -directional - args.cost_pct
        # Executable first-touch exits for the fade direction, capped at 60m.
        fade_long = cluster["direction"] == "SHORT"
        for tp in (0.8, 1.2, 2.0, 3.0):
            for sl in (0.8, 1.2, 2.0):
                value = None
                for candle in future[:12]:
                    stop_hit = candle[3] <= entry * (1 - sl / 100) if fade_long else candle[2] >= entry * (1 + sl / 100)
                    target_hit = candle[2] >= entry * (1 + tp / 100) if fade_long else candle[3] <= entry * (1 - tp / 100)
                    if stop_hit:
                        value = -sl - args.cost_pct
                        break
                    if target_hit:
                        value = tp - args.cost_pct
                        break
                if value is None:
                    directional_close = (future[11][4] / entry - 1) * 100 * (1 if fade_long else -1)
                    value = directional_close - args.cost_pct
                result[f"fixed_tp{tp}_sl{sl}"] = value
        outcomes.append(result)

    report = {
        "assumptions": {
            "market_range_ms": [first_ms, last_ms],
            "entry": "next 5m open after causal silence window (15m pump/dump, 5m liquidation)",
            "cost_pct": args.cost_pct, "cluster_gap": {"PUMP_DUMP": "15m", "LIQUIDATION": "5m"},
        },
        "raw_eligible_events": len(raw_events), "independent_clusters": len(outcomes),
        "groups": {}, "candidate_stability": {},
    }
    groups = defaultdict(list)
    for row in outcomes:
        groups[(row["event_type"], "ALL")].append(row)
        groups[(row["event_type"], row["direction"])].append(row)
        if row["event_type"] == "PUMP_DUMP":
            bucket = "COUNT_1" if row["count"] == 1 else "COUNT_2_3" if row["count"] <= 3 else "COUNT_4_PLUS"
        else:
            bucket = "USD_LT_100K" if row["notional_usd"] < 100_000 else "USD_100K_500K" if row["notional_usd"] < 500_000 else "USD_500K_PLUS"
        groups[(row["event_type"], f"{row['direction']}_{bucket}")].append(row)
    for (event_type, label), rows in sorted(groups.items()):
        report["groups"][f"{event_type}/{label}"] = {
            f"{mode}_{horizon}": metric([row[f"{mode}_{horizon}"] for row in rows if f"{mode}_{horizon}" in row])
            for mode in ("continue", "fade") for horizon in HORIZONS
        }
    candidate = [row for row in outcomes if row["event_type"] == "PUMP_DUMP" and row["direction"] == "SHORT"]
    by_week = defaultdict(list)
    by_symbol = defaultdict(list)
    for row in candidate:
        week = datetime.fromtimestamp(row["event_ms"] / 1000, timezone.utc).strftime("%Y-W%W")
        by_week[week].append(row["fade_60"])
        by_symbol[row["symbol"]].append(row["fade_60"])
    feature_groups = defaultdict(list)
    for row in candidate:
        if "oi_15m_pct" not in row:
            continue
        feature_groups["OI_FLUSH" if row["oi_15m_pct"] < 0 else "OI_BUILD"].append(row["fade_60"])
        feature_groups["LSR_DOWN" if row["lsr_15m_pct"] < 0 else "LSR_UP"].append(row["fade_60"])
        feature_groups["TAKER_BUY" if row["taker_ratio"] >= 1 else "TAKER_SELL"].append(row["fade_60"])
        feature_groups[row["confirmation_candle"]].append(row["fade_60"])
        feature_groups["VOLUME_SPIKE" if row["volume_ratio"] >= 1.5 else "VOLUME_NORMAL"].append(row["fade_60"])
        feature_groups["DUMP_4_PLUS" if row["max_move_pct"] >= 4 else "DUMP_2_4"].append(row["fade_60"])
        if row["confirmation_candle"] == "GREEN" and row["taker_ratio"] >= 1:
            feature_groups["GREEN_AND_TAKER_BUY"].append(row["fade_60"])
        if row["confirmation_candle"] == "GREEN" and row["taker_ratio"] >= 1 and row["lsr_15m_pct"] < 0:
            feature_groups["GREEN_TAKER_BUY_LSR_DOWN"].append(row["fade_60"])
        if row["max_move_pct"] >= 4 and row["taker_ratio"] >= 1:
            feature_groups["DUMP_4_PLUS_TAKER_BUY"].append(row["fade_60"])
        if row["oi_15m_pct"] >= 0 and row["lsr_15m_pct"] < 0 and row["taker_ratio"] >= 1:
            feature_groups["OI_BUILD_LSR_DOWN_TAKER_BUY"].append(row["fade_60"])
    chronological = sorted(candidate, key=lambda row: row["event_ms"])
    cut = int(len(chronological) * 0.60)
    executable_sets = {
        "ALL_DUMPS": candidate,
        "GREEN_TAKER_BUY_LSR_DOWN": [
            row for row in candidate if row.get("confirmation_candle") == "GREEN"
            and row.get("taker_ratio", 0) >= 1 and row.get("lsr_15m_pct", 0) < 0
        ],
        "DUMP_4_PLUS_TAKER_BUY": [
            row for row in candidate if row.get("max_move_pct", 0) >= 4
            and row.get("taker_ratio", 0) >= 1
        ],
        "OI_BUILD_LSR_DOWN_TAKER_BUY": [
            row for row in candidate if row.get("oi_15m_pct", -999) >= 0
            and row.get("lsr_15m_pct", 999) < 0 and row.get("taker_ratio", 0) >= 1
        ],
    }
    fixed_exits = {}
    for label, rows in executable_sets.items():
        fixed_exits[label] = {}
        for tp in (0.8, 1.2, 2.0, 3.0):
            for sl in (0.8, 1.2, 2.0):
                key = f"fixed_tp{tp}_sl{sl}"
                values = [row[key] for row in rows]
                split = int(len(values) * 0.60)
                fixed_exits[label][f"TP{tp}_SL{sl}"] = {
                    "all": trade_metric(values),
                    "development": trade_metric(values[:split]),
                    "validation": trade_metric(values[split:]),
                }
    report["candidate_stability"] = {
        "hypothesis": "fade de DUMP, isto e, entrada LONG apos terminar a rajada",
        "by_week_60m": {key: metric(values) for key, values in sorted(by_week.items())},
        "by_symbol_60m": {
            key: metric(values) for key, values in sorted(by_symbol.items()) if len(values) >= 5
        },
        "by_entry_feature_60m": {key: metric(values) for key, values in sorted(feature_groups.items())},
        "chronological_60_40": {
            "development": metric([row["fade_60"] for row in chronological[:cut]]),
            "validation": metric([row["fade_60"] for row in chronological[cut:]]),
        },
        "fixed_exit_60m": fixed_exits,
    }
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"raw": len(raw_events), "clusters": len(outcomes), "groups": report["groups"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
