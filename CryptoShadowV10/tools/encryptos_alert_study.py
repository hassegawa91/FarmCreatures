from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sqlite3
import time
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests


ALERT_RE = re.compile(
    r"(?P<date>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<time>\d{2}:\d{2}(?::\d{2})?)\s+"
    r"(?P<symbol>[A-Z0-9]+USDT)\s+Trade\s+(?P<side>UP|DOWN)\s+"
    r"1m\s+(?P<price>\d+(?:\.\d+)?)\s+Level:\s*(?P<level>\d+)",
    re.IGNORECASE,
)
LOCAL_TZ = timezone(timedelta(hours=-3))
HORIZONS = (5, 15, 30, 60, 180)
DATA_URL = (
    "https://data.binance.vision/data/futures/um/daily/klines/"
    "{symbol}/1m/{symbol}-1m-{day}.zip"
)


@dataclass(frozen=True)
class Alert:
    key: str
    message_id: int
    symbol: str
    direction: str
    level: int
    reported_price: float
    event_utc: datetime
    outer_timestamp: str


def parse_alerts(index_db: Path) -> list[Alert]:
    connection = sqlite3.connect(index_db)
    found: dict[str, Alert] = {}
    query = (
        "SELECT id, timestamp_raw, text FROM messages WHERE service=0 AND "
        "(lower(text) LIKE '%trade up%' OR lower(text) LIKE '%trade down%')"
    )
    for message_id, outer_timestamp, text in connection.execute(query):
        for match in ALERT_RE.finditer(text or ""):
            stamp = f"{match.group('date')} {match.group('time')}"
            fmt = "%d/%m/%Y %H:%M:%S" if stamp.count(":") == 2 else "%d/%m/%Y %H:%M"
            local = datetime.strptime(stamp, fmt).replace(tzinfo=LOCAL_TZ)
            event_utc = local.astimezone(timezone.utc)
            symbol = match.group("symbol").upper()
            direction = "LONG" if match.group("side").upper() == "UP" else "SHORT"
            key = f"{event_utc.isoformat()}|{symbol}|{direction}|{match.group('price')}|{match.group('level')}"
            found.setdefault(
                key,
                Alert(
                    key=key,
                    message_id=int(message_id),
                    symbol=symbol,
                    direction=direction,
                    level=int(match.group("level")),
                    reported_price=float(match.group("price")),
                    event_utc=event_utc,
                    outer_timestamp=outer_timestamp or "",
                ),
            )
    connection.close()
    return sorted(found.values(), key=lambda item: item.event_utc)


def required_days(alerts: list[Alert]) -> set[tuple[str, str]]:
    result: set[tuple[str, str]] = set()
    for alert in alerts:
        day = alert.event_utc.date()
        result.add((alert.symbol, day.isoformat()))
        if alert.event_utc.hour >= 21:
            result.add((alert.symbol, (day + timedelta(days=1)).isoformat()))
    return result


def download_one(cache: Path, symbol: str, day: str) -> tuple[str, str, str]:
    target = cache / symbol / f"{day}.zip"
    missing = cache / symbol / f"{day}.missing"
    if target.exists():
        return symbol, day, "CACHED"
    if missing.exists():
        return symbol, day, "MISSING_CACHED"
    target.parent.mkdir(parents=True, exist_ok=True)
    url = DATA_URL.format(symbol=symbol, day=day)
    last_error = ""
    for attempt in range(3):
        try:
            response = requests.get(url, timeout=30)
            if response.status_code == 404:
                missing.write_text(url, encoding="utf-8")
                return symbol, day, "MISSING"
            response.raise_for_status()
            target.write_bytes(response.content)
            return symbol, day, "DOWNLOADED"
        except Exception as exc:  # network retry is intentionally narrow
            last_error = str(exc)
            time.sleep(0.5 * (attempt + 1))
    return symbol, day, f"ERROR:{last_error[:100]}"


def download_days(cache: Path, days: set[tuple[str, str]], workers: int) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(download_one, cache, symbol, day) for symbol, day in sorted(days)]
        for future in as_completed(futures):
            _, _, status = future.result()
            counts[status.split(":", 1)[0]] += 1
    return dict(counts)


def load_day(cache: Path, symbol: str, day: str) -> list[tuple[int, float, float, float, float]]:
    path = cache / symbol / f"{day}.zip"
    if not path.exists():
        return []
    with zipfile.ZipFile(path) as archive:
        name = archive.namelist()[0]
        raw = archive.read(name).decode("utf-8-sig")
    candles = []
    for row in csv.reader(io.StringIO(raw)):
        if not row or not row[0].isdigit():
            continue
        timestamp = int(row[0])
        # Binance archives migrated from milliseconds to microseconds in 2025.
        if timestamp > 10**14:
            timestamp //= 1000
        candles.append((timestamp, float(row[1]), float(row[2]), float(row[3]), float(row[4])))
    return candles


def outcome(alert: Alert, candles: list[tuple[int, float, float, float, float]]) -> dict[str, object] | None:
    event_ms = int(alert.event_utc.timestamp() * 1000)
    # Enter at the next complete minute open: avoids using a price known before forwarding.
    usable = [row for row in candles if row[0] >= event_ms + 60_000 and row[0] <= event_ms + 181 * 60_000]
    if len(usable) < 5:
        return None
    entry = usable[0][1]
    sign = 1.0 if alert.direction == "LONG" else -1.0
    result: dict[str, object] = {"entry_price": entry, "price_gap_pct": (entry / alert.reported_price - 1) * 100}
    for minutes in HORIZONS:
        window = usable[:minutes]
        if len(window) < minutes:
            continue
        last = window[-1][4]
        directional_return = sign * (last / entry - 1.0) * 100
        if alert.direction == "LONG":
            mfe = (max(row[2] for row in window) / entry - 1.0) * 100
            mae = (min(row[3] for row in window) / entry - 1.0) * 100
        else:
            mfe = (1.0 - min(row[3] for row in window) / entry) * 100
            mae = (1.0 - max(row[2] for row in window) / entry) * 100
        result[f"ret_{minutes}"] = directional_return
        result[f"mfe_{minutes}"] = mfe
        result[f"mae_{minutes}"] = mae
    return result


def schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS alerts (
          alert_key TEXT PRIMARY KEY, message_id INTEGER, symbol TEXT, direction TEXT,
          level INTEGER, reported_price REAL, event_utc TEXT, outer_timestamp TEXT,
          burst_index INTEGER, burst_count_10m INTEGER, entry_price REAL, price_gap_pct REAL,
          ret_5 REAL, mfe_5 REAL, mae_5 REAL, ret_15 REAL, mfe_15 REAL, mae_15 REAL,
          ret_30 REAL, mfe_30 REAL, mae_30 REAL, ret_60 REAL, mfe_60 REAL, mae_60 REAL,
          ret_180 REAL, mfe_180 REAL, mae_180 REAL, evaluated_utc TEXT
        );
        """
    )


def median(values: list[float]) -> float:
    values = sorted(values)
    if not values:
        return float("nan")
    middle = len(values) // 2
    return values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2


def summarize(rows: list[dict[str, object]], field: str) -> dict[str, float]:
    values = [float(row[field]) for row in rows if row.get(field) is not None]
    return {
        "n": len(values),
        "mean": sum(values) / len(values) if values else float("nan"),
        "median": median(values),
        "positive_pct": 100 * sum(value > 0 for value in values) / len(values) if values else float("nan"),
    }


def write_report(path: Path, alerts: list[Alert], rows: list[dict[str, object]], downloads: dict[str, int]) -> None:
    lines = [
        "# Estudo dos alertas históricos Trade UP/DOWN — Encryptos completo",
        "",
        "Entradas são simuladas na abertura do minuto seguinte ao alerta interno. Retornos são",
        "direcionais e brutos; não representam execução real nem validam OI/LSR isoladamente.",
        "",
        f"- Alertas únicos extraídos: {len(alerts):,}",
        f"- Alertas com candles avaliáveis: {len(rows):,}",
        f"- Downloads/cache: `{json.dumps(downloads, ensure_ascii=False)}`",
        "",
        "## Resultado agregado",
        "",
        "| Horizonte | N | Retorno médio | Mediana | Direção correta | MFE médio | MAE médio |",
        "|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for horizon in HORIZONS:
        ret = summarize(rows, f"ret_{horizon}")
        mfe = summarize(rows, f"mfe_{horizon}")
        mae = summarize(rows, f"mae_{horizon}")
        lines.append(
            f"| {horizon}m | {int(ret['n'])} | {ret['mean']:.4f}% | {ret['median']:.4f}% | "
            f"{ret['positive_pct']:.2f}% | {mfe['mean']:.4f}% | {mae['mean']:.4f}% |"
        )
    lines.extend(["", "## Cortes principais em 60 minutos", ""])
    groups: list[tuple[str, list[dict[str, object]]]] = []
    for direction in ("LONG", "SHORT"):
        groups.append((direction, [row for row in rows if row["direction"] == direction]))
    for level_group, predicate in (
        ("nível 1–3", lambda row: int(row["level"]) <= 3),
        ("nível 5", lambda row: int(row["level"]) == 5),
        ("nível 6", lambda row: int(row["level"]) == 6),
        ("1º alerta da rajada", lambda row: int(row["burst_index"]) == 1),
        ("2º alerta da rajada", lambda row: int(row["burst_index"]) == 2),
        ("3º+ alerta da rajada", lambda row: int(row["burst_index"]) >= 3),
    ):
        groups.append((level_group, [row for row in rows if predicate(row)]))
    lines.extend([
        "| Corte | N | Retorno médio | Mediana | Direção correta | MFE médio | MAE médio |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ])
    for label, group in groups:
        ret = summarize(group, "ret_60")
        mfe = summarize(group, "mfe_60")
        mae = summarize(group, "mae_60")
        lines.append(
            f"| {label} | {int(ret['n'])} | {ret['mean']:.4f}% | {ret['median']:.4f}% | "
            f"{ret['positive_pct']:.2f}% | {mfe['mean']:.4f}% | {mae['mean']:.4f}% |"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("index_db", type=Path)
    parser.add_argument("output_db", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    alerts = parse_alerts(args.index_db)
    downloads = download_days(args.cache, required_days(alerts), max(1, args.workers))
    by_symbol_day: dict[tuple[str, str], list[tuple[int, float, float, float, float]]] = {}
    rows: list[dict[str, object]] = []
    recent: dict[tuple[str, str], list[datetime]] = defaultdict(list)
    now = datetime.now(timezone.utc).isoformat()
    for alert in alerts:
        key = (alert.symbol, alert.direction)
        recent[key] = [stamp for stamp in recent[key] if alert.event_utc - stamp <= timedelta(minutes=10)]
        recent[key].append(alert.event_utc)
        burst_index = len(recent[key])
        days = [alert.event_utc.date().isoformat(), (alert.event_utc.date() + timedelta(days=1)).isoformat()]
        candles = []
        for day in days:
            cache_key = (alert.symbol, day)
            if cache_key not in by_symbol_day:
                by_symbol_day[cache_key] = load_day(args.cache, *cache_key)
            candles.extend(by_symbol_day[cache_key])
        values = outcome(alert, candles)
        if values is None:
            continue
        rows.append({
            "alert_key": alert.key, "message_id": alert.message_id, "symbol": alert.symbol,
            "direction": alert.direction, "level": alert.level, "reported_price": alert.reported_price,
            "event_utc": alert.event_utc.isoformat(), "outer_timestamp": alert.outer_timestamp,
            "burst_index": burst_index, "burst_count_10m": burst_index, **values,
            "evaluated_utc": now,
        })

    connection = sqlite3.connect(args.output_db)
    schema(connection)
    columns = [row[1] for row in connection.execute("PRAGMA table_info(alerts)")]
    placeholders = ",".join("?" for _ in columns)
    connection.executemany(
        f"INSERT OR REPLACE INTO alerts ({','.join(columns)}) VALUES ({placeholders})",
        [[row.get(column) for column in columns] for row in rows],
    )
    connection.commit()
    connection.close()
    write_report(args.report, alerts, rows, downloads)
    print(json.dumps({"alerts": len(alerts), "evaluated": len(rows), "downloads": downloads}, ensure_ascii=False))


if __name__ == "__main__":
    main()
