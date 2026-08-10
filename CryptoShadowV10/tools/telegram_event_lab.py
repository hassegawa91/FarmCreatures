from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


NUMBER = r"(?:\d+(?:[.,]\d+)?(?:[KMB])?)"
PUMP_DUMP_RE = re.compile(
    rf"\b(?P<side>PUMP|DUMP)\s*:\s*(?P<symbol>[A-Z0-9\u4e00-\u9fff]+USDT)\s*"
    rf"(?P<move>[+-]?\d+(?:[.,]\d+)?)%\s*/\s*price\s*(?P<price>\d+(?:[.,]\d+)?)",
    re.IGNORECASE,
)
LIQUIDATION_RE = re.compile(
    rf"#(?P<symbol>[A-Z0-9]+)\s+Liquidated\s+(?P<side>Long|Short)\s*:\s*\$(?P<notional>{NUMBER})"
    rf"\s+at\s+\$(?P<price>\d+(?:[.,]\d+)?)",
    re.IGNORECASE,
)
SIGNAL_ID_RE = re.compile(r"\bSIGNAL\s+ID\s*:\s*#?(?P<signal_id>\d+)", re.IGNORECASE)
SIGNAL_SYMBOL_RE = re.compile(r"\bCOIN\s*:\s*\$?(?P<symbol>[A-Z0-9]+)\s*/?\s*USDT", re.IGNORECASE)
SIGNAL_DIRECTION_RE = re.compile(r"\bDirection\s*:\s*(?P<direction>LONG|SHORT)", re.IGNORECASE)
ENTRY_RE = re.compile(r"\bENTRY\s*:\s*(?P<values>[0-9.,\s\-–]+)", re.IGNORECASE)
STOP_RE = re.compile(r"\bSTOP\s*LOSS\s*:\s*(?P<value>\d+(?:[.,]\d+)?)", re.IGNORECASE)
TARGET_BLOCK_RE = re.compile(
    r"\bTARGETS?\b(?P<values>.*?)(?=\b(?:STOP\s*LOSS|All rights|Received|Direction|ENTRY)\b|$)",
    re.IGNORECASE | re.DOTALL,
)
NEWS_TERMS = re.compile(
    r"\b(?:FED|FOMC|CPI|PCE|PAYROLL|JOBS|GDP|INFLATION|INFLACAO|RATE CUT|RATE HIKE|"
    r"INTEREST RATE|ETF|SEC|TREASURY|LIQUIDITY|LIQUIDEZ|DOLLAR INDEX|DXY)\b",
    re.IGNORECASE,
)
NUMERIC_RE = re.compile(r"\d+(?:[.,]\d+)?")


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    return " ".join(value.split())


def number(value: str) -> float:
    clean = value.strip().upper().replace(",", ".")
    scale = 1.0
    if clean[-1:] in {"K", "M", "B"}:
        scale = {"K": 1e3, "M": 1e6, "B": 1e9}[clean[-1]]
        clean = clean[:-1]
    return float(clean) * scale


def numbers(value: str) -> list[float]:
    return [number(item) for item in NUMERIC_RE.findall(value)]


@dataclass(frozen=True)
class ExternalEvent:
    event_type: str
    symbol: str | None
    direction: str | None
    event_key: str
    payload: dict[str, object]


def _key(parts: list[object]) -> str:
    canonical = "|".join(str(part).upper() for part in parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_events(text: str) -> list[ExternalEvent]:
    clean = normalize_text(text)
    events: list[ExternalEvent] = []

    for match in PUMP_DUMP_RE.finditer(clean):
        side = match.group("side").upper()
        symbol = match.group("symbol").upper()
        move = float(match.group("move").replace(",", "."))
        price = number(match.group("price"))
        events.append(ExternalEvent(
            "PUMP_DUMP", symbol, "LONG" if side == "PUMP" else "SHORT",
            _key(["PUMP_DUMP", symbol, side, price, move]),
            {"side": side, "move_pct": move, "reported_price": price},
        ))

    for match in LIQUIDATION_RE.finditer(clean):
        liquidated_side = match.group("side").upper()
        symbol = match.group("symbol").upper() + "USDT"
        notional = number(match.group("notional"))
        price = number(match.group("price"))
        events.append(ExternalEvent(
            "LIQUIDATION", symbol, "SHORT" if liquidated_side == "LONG" else "LONG",
            _key(["LIQUIDATION", symbol, liquidated_side, price, notional]),
            {
                "liquidated_side": liquidated_side,
                "notional_usd": notional,
                "reported_price": price,
            },
        ))

    signal_id = SIGNAL_ID_RE.search(clean)
    signal_symbol = SIGNAL_SYMBOL_RE.search(clean)
    signal_direction = SIGNAL_DIRECTION_RE.search(clean)
    if signal_id and signal_symbol and signal_direction and "TARGET" in clean.upper():
        symbol = signal_symbol.group("symbol").upper() + "USDT"
        direction = signal_direction.group("direction").upper()
        entry_match = ENTRY_RE.search(clean)
        stop_match = STOP_RE.search(clean)
        target_match = TARGET_BLOCK_RE.search(clean)
        entry = numbers(entry_match.group("values"))[:2] if entry_match else []
        targets = numbers(target_match.group("values")) if target_match else []
        stop = number(stop_match.group("value")) if stop_match else None
        identifier = signal_id.group("signal_id")
        events.append(ExternalEvent(
            "STRUCTURED_SIGNAL", symbol, direction,
            _key(["STRUCTURED_SIGNAL", identifier, symbol, direction, entry, stop, targets]),
            {
                "signal_id": identifier,
                "entry": entry,
                "stop": stop,
                "targets": targets,
                "setup": "BREAKOUT_RETEST" if "BREAKOUT RETEST" in clean.upper()
                else "BREAKOUT" if "BREAKOUT" in clean.upper() else "UNSPECIFIED",
            },
        ))

    if not events and NEWS_TERMS.search(clean):
        events.append(ExternalEvent(
            "INSTITUTIONAL_NEWS", None, None,
            _key(["INSTITUTIONAL_NEWS", clean[:500]]),
            {"headline": clean[:1000]},
        ))
    return events


def schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS sources (
            source_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            source_class TEXT NOT NULL,
            independence_group TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'RESEARCH_ONLY'
        );
        CREATE TABLE IF NOT EXISTS external_events (
            id INTEGER PRIMARY KEY,
            source_id TEXT NOT NULL,
            source_message_id TEXT NOT NULL,
            timestamp_raw TEXT NOT NULL,
            event_type TEXT NOT NULL,
            symbol TEXT,
            direction TEXT,
            event_key TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            raw_text TEXT NOT NULL,
            ingested_utc TEXT NOT NULL,
            UNIQUE(source_id, source_message_id, event_key)
        );
        CREATE INDEX IF NOT EXISTS external_events_time ON external_events(timestamp_raw);
        CREATE INDEX IF NOT EXISTS external_events_symbol ON external_events(symbol, event_type);
        CREATE INDEX IF NOT EXISTS external_events_dedupe ON external_events(event_key);
        CREATE TABLE IF NOT EXISTS event_outcomes (
            event_id INTEGER NOT NULL,
            horizon_minutes INTEGER NOT NULL,
            entry_price REAL NOT NULL,
            return_pct REAL NOT NULL,
            mfe_pct REAL NOT NULL,
            mae_pct REAL NOT NULL,
            cost_pct REAL NOT NULL,
            evaluated_utc TEXT NOT NULL,
            PRIMARY KEY(event_id, horizon_minutes)
        );
        """
    )


def ingest_index(
    source_database: Path,
    lab_database: Path,
    source_id: str,
    title: str,
    source_class: str,
    independence_group: str,
) -> dict[str, int]:
    source = sqlite3.connect(source_database)
    target = sqlite3.connect(lab_database)
    schema(target)
    target.execute(
        "INSERT OR REPLACE INTO sources VALUES(?,?,?,?,COALESCE((SELECT status FROM sources WHERE source_id=?),'RESEARCH_ONLY'))",
        (source_id, title, source_class, independence_group, source_id),
    )
    inserted = parsed = scanned = 0
    now = datetime.now(timezone.utc).isoformat()
    for message_id, timestamp, text in source.execute(
        "SELECT COALESCE(source_message_id, CAST(id AS TEXT)), COALESCE(timestamp_raw,''), COALESCE(text,'') "
        "FROM messages WHERE service=0 AND length(trim(COALESCE(text,'')))>0"
    ):
        scanned += 1
        events = parse_events(text)
        parsed += len(events)
        for event in events:
            cursor = target.execute(
                """INSERT OR IGNORE INTO external_events(
                    source_id,source_message_id,timestamp_raw,event_type,symbol,direction,
                    event_key,payload_json,raw_text,ingested_utc
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    source_id, str(message_id), timestamp, event.event_type, event.symbol,
                    event.direction, event.event_key, json.dumps(event.payload, ensure_ascii=False),
                    text, now,
                ),
            )
            inserted += max(cursor.rowcount, 0)
    target.commit()
    counts = dict(target.execute(
        "SELECT event_type, COUNT(*) FROM external_events WHERE source_id=? GROUP BY event_type",
        (source_id,),
    ).fetchall())
    source.close()
    target.close()
    return {"scanned_messages": scanned, "parsed_events": parsed, "inserted_events": inserted, **counts}


def main() -> None:
    parser = argparse.ArgumentParser(description="Normaliza eventos externos exportados do Telegram.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    ingest = subparsers.add_parser("ingest-index")
    ingest.add_argument("source_database", type=Path)
    ingest.add_argument("lab_database", type=Path)
    ingest.add_argument("--source-id", required=True)
    ingest.add_argument("--title", required=True)
    ingest.add_argument("--class", dest="source_class", required=True)
    ingest.add_argument("--independence-group", required=True)
    args = parser.parse_args()
    result = ingest_index(
        args.source_database, args.lab_database, args.source_id, args.title,
        args.source_class, args.independence_group,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
