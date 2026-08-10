from __future__ import annotations

import argparse
import sqlite3
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

from encryptos_alert_study import load_day, parse_alerts


TPS = (0.6, 1.0, 1.5, 2.0)
SLS = (0.6, 1.0, 1.5)
COST_PCT = 0.12


def simulate(direction: str, entry: float, candles, tp: float, sl: float) -> tuple[float, str, int]:
    for minute, (_, _, high, low, close) in enumerate(candles[:180], start=1):
        if direction == "LONG":
            stop_hit = low <= entry * (1 - sl / 100)
            target_hit = high >= entry * (1 + tp / 100)
        else:
            stop_hit = high >= entry * (1 + sl / 100)
            target_hit = low <= entry * (1 - tp / 100)
        # Conservative when OHLC cannot reveal which boundary traded first.
        if stop_hit:
            return -sl - COST_PCT, "SL", minute
        if target_hit:
            return tp - COST_PCT, "TP", minute
    close = candles[min(len(candles), 180) - 1][4]
    sign = 1 if direction == "LONG" else -1
    return sign * (close / entry - 1) * 100 - COST_PCT, "TIME", min(len(candles), 180)


def metrics(values: list[float]) -> tuple[int, float, float, float, float]:
    if not values:
        return 0, 0, 0, 0, 0
    wins = sum(value > 0 for value in values)
    gross_win = sum(value for value in values if value > 0)
    gross_loss = -sum(value for value in values if value < 0)
    return len(values), sum(values) / len(values), wins * 100 / len(values), gross_win / gross_loss if gross_loss else 99.0, sum(values)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("index_db", type=Path)
    parser.add_argument("cache", type=Path)
    parser.add_argument("output_db", type=Path)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()

    alerts = parse_alerts(args.index_db)
    cache_rows = {}
    recent = defaultdict(list)
    cluster_number = defaultdict(int)
    simulations = []
    for alert in alerts:
        state = (alert.symbol, alert.direction)
        previous = recent[state][-1] if recent[state] else None
        if previous is None or alert.event_utc - previous > timedelta(minutes=10):
            cluster_number[state] += 1
            recent[state] = []
        recent[state].append(alert.event_utc)
        burst_index = len(recent[state])
        cluster_key = f"{alert.symbol}|{alert.direction}|{cluster_number[state]}"
        candles = []
        for day in (alert.event_utc.date(), alert.event_utc.date() + timedelta(days=1)):
            key = (alert.symbol, day.isoformat())
            if key not in cache_rows:
                cache_rows[key] = load_day(args.cache, *key)
            candles.extend(cache_rows[key])
        event_ms = int(alert.event_utc.timestamp() * 1000)
        future = [row for row in candles if event_ms + 60_000 <= row[0] <= event_ms + 181 * 60_000]
        if len(future) < 5:
            continue
        entry = future[0][1]
        for tp in TPS:
            for sl in SLS:
                net, reason, minutes = simulate(alert.direction, entry, future, tp, sl)
                simulations.append((alert.key, cluster_key, burst_index, alert.symbol, alert.direction,
                                    alert.level, tp, sl, net, reason, minutes))

    connection = sqlite3.connect(args.output_db)
    connection.executescript("""
      DROP TABLE IF EXISTS exit_simulations;
      CREATE TABLE exit_simulations(
        alert_key TEXT, cluster_key TEXT, burst_index INTEGER, symbol TEXT, direction TEXT,
        level INTEGER, tp_pct REAL, sl_pct REAL, net_pct REAL, reason TEXT, minutes INTEGER,
        PRIMARY KEY(alert_key,tp_pct,sl_pct)
      );
      CREATE INDEX exit_sim_filter ON exit_simulations(tp_pct,sl_pct,burst_index,direction,level);
    """)
    connection.executemany("INSERT INTO exit_simulations VALUES(?,?,?,?,?,?,?,?,?,?,?)", simulations)
    connection.commit()

    lines = [
        "# Saídas fixas sobre alertas Encryptos",
        "",
        f"Custo conservador por round-trip: {COST_PCT:.2f}%. Em candle que toca TP e SL, o SL vence.",
        "Cada linha abaixo usa apenas um índice da rajada por cluster, evitando contar o mesmo pump várias vezes.",
        "",
        "| Entrada | TP | SL | N | Retorno médio líquido | Win rate | Profit factor | Soma |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for burst in (1, 2, 3, 4):
        for tp in TPS:
            for sl in SLS:
                values = [row[8] for row in simulations if row[2] == burst and row[6] == tp and row[7] == sl]
                n, mean, win_rate, pf, total = metrics(values)
                lines.append(f"| {burst}º alerta | {tp:.1f}% | {sl:.1f}% | {n} | {mean:.4f}% | {win_rate:.2f}% | {pf:.3f} | {total:.2f}% |")
    lines.extend(["", "## Melhores cortes sem escolher ativo depois do fato", ""])
    candidates = []
    for direction in ("ALL", "LONG", "SHORT"):
        for level_min in (1, 3, 5, 6):
            for burst in (1, 2, 3, 4):
                for tp in TPS:
                    for sl in SLS:
                        values = [
                            row[8] for row in simulations
                            if row[2] == burst and row[5] >= level_min and row[6] == tp and row[7] == sl
                            and (direction == "ALL" or row[4] == direction)
                        ]
                        n, mean, win_rate, pf, total = metrics(values)
                        if n >= 30:
                            candidates.append((pf, mean, n, direction, level_min, burst, tp, sl, win_rate, total))
    lines.extend([
        "| Direção | Nível mín. | Alerta | TP | SL | N | Média | Win rate | PF |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for pf, mean, n, direction, level_min, burst, tp, sl, win_rate, _ in sorted(candidates, reverse=True)[:20]:
        lines.append(f"| {direction} | {level_min} | {burst}º | {tp:.1f}% | {sl:.1f}% | {n} | {mean:.4f}% | {win_rate:.2f}% | {pf:.3f} |")
    args.report.write_text("\n".join(lines), encoding="utf-8")
    connection.close()
    print(f"simulations={len(simulations)} report={args.report}")


if __name__ == "__main__":
    main()
