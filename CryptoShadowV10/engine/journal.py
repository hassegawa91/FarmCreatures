from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from engine.binance import MarketSnapshot


class TradeJournal:
    def __init__(
        self, path: str | Path, fee_pct_per_side: float,
        reference_management: dict[str, Any] | None = None,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fee_pct_per_side = float(fee_pct_per_side)
        self.reference_management = reference_management or {}
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._create_schema()

    def _create_schema(self) -> None:
        with self.db:
            self.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp_ms INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    opened_at_ms INTEGER NOT NULL,
                    closed_at_ms INTEGER,
                    symbol TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'REFERENCE',
                    direction TEXT NOT NULL,
                    setup TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    stop_price REAL NOT NULL,
                    target_price REAL NOT NULL,
                    risk_pct REAL NOT NULL,
                    exit_price REAL,
                    exit_reason TEXT,
                    gross_pct REAL,
                    net_pct REAL,
                    result_r REAL,
                    mfe_pct REAL NOT NULL DEFAULT 0,
                    mae_pct REAL NOT NULL DEFAULT 0,
                    current_stop_price REAL,
                    partial_taken INTEGER NOT NULL DEFAULT 0,
                    partial_fraction REAL NOT NULL DEFAULT 0,
                    partial_at_r REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'OPEN',
                    evidence_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status);
                CREATE INDEX IF NOT EXISTS idx_events_time ON events(timestamp_ms);
                CREATE TABLE IF NOT EXISTS executions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp_ms INTEGER NOT NULL,
                    mode TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    ok INTEGER NOT NULL,
                    signal_json TEXT NOT NULL,
                    result_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS execution_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    execution_id INTEGER NOT NULL UNIQUE,
                    closed_at_ms INTEGER NOT NULL,
                    mode TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL NOT NULL,
                    quantity REAL NOT NULL,
                    realized_pnl REAL NOT NULL,
                    commission REAL NOT NULL,
                    net_pnl REAL NOT NULL,
                    net_pct REAL NOT NULL,
                    result_r REAL NOT NULL,
                    exit_reason TEXT NOT NULL,
                    trades_json TEXT NOT NULL,
                    FOREIGN KEY(execution_id) REFERENCES executions(id)
                );
                CREATE TABLE IF NOT EXISTS feature_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp_ms INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    price REAL NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    diagnostics_json TEXT NOT NULL,
                    return_5m_pct REAL,
                    return_15m_pct REAL,
                    return_30m_pct REAL,
                    return_60m_pct REAL,
                    updated_at_ms INTEGER,
                    UNIQUE(timestamp_ms, symbol)
                );
                CREATE INDEX IF NOT EXISTS idx_feature_observations_pending
                    ON feature_observations(symbol,timestamp_ms);
                """
            )
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(signals)").fetchall()}
            migrations = {
                "source": "TEXT NOT NULL DEFAULT 'REFERENCE'",
                "current_stop_price": "REAL",
                "partial_taken": "INTEGER NOT NULL DEFAULT 0",
                "partial_fraction": "REAL NOT NULL DEFAULT 0",
                "partial_at_r": "REAL NOT NULL DEFAULT 0",
            }
            for name, declaration in migrations.items():
                if name not in columns:
                    self.db.execute(f"ALTER TABLE signals ADD COLUMN {name} {declaration}")

    def close(self) -> None:
        with self.lock:
            self.db.close()

    def record_event(self, event: Any) -> None:
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO events(timestamp_ms,type,symbol,direction,reason,payload_json) VALUES(?,?,?,?,?,?)",
                (event.timestamp_ms, event.type, event.symbol, event.direction, event.reason, json.dumps(event.payload, ensure_ascii=False)),
            )

    def record_feature_observation(
        self, snapshot: MarketSnapshot, diagnostics: list[dict[str, Any]],
    ) -> None:
        """Persist heavy-candidate evidence independently from trade execution."""
        with self.lock, self.db:
            self.db.execute(
                """INSERT OR IGNORE INTO feature_observations(
                    timestamp_ms,symbol,price,snapshot_json,diagnostics_json
                ) VALUES(?,?,?,?,?)""",
                (
                    int(snapshot.timestamp_ms), snapshot.symbol, float(snapshot.price),
                    json.dumps(snapshot.to_dict(), ensure_ascii=False),
                    json.dumps(diagnostics, ensure_ascii=False),
                ),
            )

    def update_feature_outcomes(self, contexts: dict[str, dict[str, Any]], now_ms: int) -> int:
        """Label observations at the first available scan after each forward horizon."""
        horizons = ((5, "return_5m_pct"), (15, "return_15m_pct"),
                    (30, "return_30m_pct"), (60, "return_60m_pct"))
        with self.lock, self.db:
            rows = self.db.execute(
                """SELECT id,timestamp_ms,symbol,price,return_5m_pct,return_15m_pct,
                          return_30m_pct,return_60m_pct
                   FROM feature_observations
                   WHERE timestamp_ms<=? AND (
                       return_5m_pct IS NULL OR return_15m_pct IS NULL OR
                       return_30m_pct IS NULL OR return_60m_pct IS NULL
                   )""",
                (int(now_ms) - 5 * 60_000,),
            ).fetchall()
            updated = 0
            for row in rows:
                current_price = float((contexts.get(str(row["symbol"])) or {}).get("price") or 0.0)
                origin = float(row["price"] or 0.0)
                if current_price <= 0 or origin <= 0:
                    continue
                assignments: list[str] = []
                values: list[float | int] = []
                for minutes, column in horizons:
                    if row[column] is None and int(now_ms) >= int(row["timestamp_ms"]) + minutes * 60_000:
                        assignments.append(f"{column}=?")
                        values.append((current_price - origin) / origin * 100.0)
                if not assignments:
                    continue
                assignments.append("updated_at_ms=?")
                values.extend((int(now_ms), int(row["id"])))
                self.db.execute(
                    f"UPDATE feature_observations SET {','.join(assignments)} WHERE id=?", values,
                )
                updated += 1
            return updated

    def feature_observation_summary(self) -> dict[str, Any]:
        with self.lock:
            row = self.db.execute(
                """SELECT COUNT(*) total,
                          SUM(return_5m_pct IS NOT NULL) labeled_5m,
                          SUM(return_15m_pct IS NOT NULL) labeled_15m,
                          SUM(return_30m_pct IS NOT NULL) labeled_30m,
                          SUM(return_60m_pct IS NOT NULL) labeled_60m
                   FROM feature_observations"""
            ).fetchone()
        return {key: int(row[key] or 0) for key in row.keys()}

    def open_count(self, source: str = "REFERENCE") -> int:
        with self.lock:
            return int(self.db.execute(
                "SELECT COUNT(*) FROM signals WHERE status='OPEN' AND source=?", (source,)
            ).fetchone()[0])

    def open_symbols(self) -> list[str]:
        with self.lock:
            return [row[0] for row in self.db.execute(
                "SELECT symbol FROM signals WHERE status='OPEN' AND source='REFERENCE'"
            ).fetchall()]

    def record_signal(
        self, signal: dict[str, Any], max_concurrent: int, source: str = "REFERENCE",
    ) -> tuple[bool, str]:
        with self.lock, self.db:
            if self.open_count(source) >= max_concurrent:
                return False, "max_concurrent_shadow_positions"
            duplicate = self.db.execute(
                "SELECT 1 FROM signals WHERE status='OPEN' AND symbol=? AND source=?",
                (signal["symbol"], source),
            ).fetchone()
            if duplicate:
                return False, "symbol_already_open"
            self.db.execute(
                """INSERT INTO signals(
                    opened_at_ms,symbol,source,direction,setup,entry_price,stop_price,target_price,risk_pct,
                    current_stop_price,evidence_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    signal["timestamp_ms"], signal["symbol"], source, signal["direction"], signal["setup"],
                    signal["entry_price"], signal["stop_price"], signal["target_price"], signal["risk_pct"],
                    signal["stop_price"],
                    json.dumps(signal.get("evidence") or {}, ensure_ascii=False),
                ),
            )
            return True, "opened_reference"

    def update_position(self, snapshot: MarketSnapshot) -> dict[str, Any] | None:
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT * FROM signals WHERE status='OPEN' AND source='REFERENCE' AND symbol=? ORDER BY id DESC LIMIT 1",
                (snapshot.symbol,),
            ).fetchone()
            if not row:
                return None
            long = row["direction"] == "LONG"
            sign = 1.0 if long else -1.0
            move_pct = (snapshot.price - row["entry_price"]) / row["entry_price"] * 100.0 * sign
            mfe = max(float(row["mfe_pct"]), move_pct)
            mae = max(float(row["mae_pct"]), -move_pct)
            risk_pct = float(row["risk_pct"] or 0.0)
            partial_at_r = float(self.reference_management.get("partial_at_r", 1.0))
            partial_fraction = float(self.reference_management.get("partial_fraction", 0.40))
            if not bool(row["partial_taken"]) and risk_pct > 0 and move_pct >= partial_at_r * risk_pct:
                buffer = float(self.reference_management.get("runner_structure_buffer_pct", 0.12)) / 100.0
                lows = [value for value in (snapshot.candle_low, snapshot.previous_candle_low) if value > 0]
                highs = [value for value in (snapshot.candle_high, snapshot.previous_candle_high) if value > 0]
                initial_stop = float(row["stop_price"])
                if long:
                    structural = min(lows) * (1.0 - buffer) if lows else initial_stop
                    managed_stop = max(initial_stop, structural)
                    managed_stop = min(managed_stop, snapshot.price * 0.999)
                else:
                    structural = max(highs) * (1.0 + buffer) if highs else initial_stop
                    managed_stop = min(initial_stop, structural)
                    managed_stop = max(managed_stop, snapshot.price * 1.001)
                self.db.execute(
                    """UPDATE signals SET partial_taken=1,partial_fraction=?,partial_at_r=?,
                       current_stop_price=?,mfe_pct=?,mae_pct=? WHERE id=?""",
                    (partial_fraction, partial_at_r, managed_stop, mfe, mae, row["id"]),
                )
                return None
            reason = None
            exit_price = snapshot.price
            active_stop = float(row["current_stop_price"] or row["stop_price"])
            if (long and snapshot.price <= active_stop) or (not long and snapshot.price >= active_stop):
                reason, exit_price = "REFERENCE_STOP", active_stop
            elif (long and snapshot.price >= row["target_price"]) or (not long and snapshot.price <= row["target_price"]):
                reason, exit_price = "REFERENCE_TARGET", float(row["target_price"])
            if not reason:
                self.db.execute("UPDATE signals SET mfe_pct=?,mae_pct=? WHERE id=?", (mfe, mae, row["id"]))
                return None
            final_move = (exit_price - row["entry_price"]) / row["entry_price"] * 100.0 * sign
            if bool(row["partial_taken"]):
                fraction = float(row["partial_fraction"] or partial_fraction)
                realized_move = float(row["partial_at_r"] or partial_at_r) * risk_pct
                gross = fraction * realized_move + (1.0 - fraction) * final_move
            else:
                gross = final_move
            net = gross - 2.0 * self.fee_pct_per_side
            result_r = net / float(row["risk_pct"]) if row["risk_pct"] else 0.0
            self.db.execute(
                """UPDATE signals SET closed_at_ms=?,exit_price=?,exit_reason=?,gross_pct=?,net_pct=?,result_r=?,
                   mfe_pct=?,mae_pct=?,status='CLOSED' WHERE id=?""",
                (snapshot.timestamp_ms, exit_price, reason, gross, net, result_r, mfe, mae, row["id"]),
            )
            return {
                "id": row["id"], "symbol": row["symbol"], "direction": row["direction"],
                "source": row["source"], "setup": row["setup"], "reason": reason,
                "net_pct": net, "result_r": result_r, "partial_taken": bool(row["partial_taken"]),
            }

    def close_reference(
        self, symbol: str, price: float, timestamp_ms: int, reason: str = "REFERENCE_REPLACED",
    ) -> dict[str, Any] | None:
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT * FROM signals WHERE status='OPEN' AND source='REFERENCE' AND symbol=? ORDER BY id DESC LIMIT 1",
                (symbol,),
            ).fetchone()
            if not row:
                return None
            sign = 1.0 if row["direction"] == "LONG" else -1.0
            final_move = (float(price) - row["entry_price"]) / row["entry_price"] * 100.0 * sign
            risk_pct = float(row["risk_pct"] or 0.0)
            if bool(row["partial_taken"]):
                fraction = float(row["partial_fraction"] or 0.40)
                gross = fraction * float(row["partial_at_r"] or 1.0) * risk_pct + (1.0 - fraction) * final_move
            else:
                gross = final_move
            net = gross - 2.0 * self.fee_pct_per_side
            result_r = net / risk_pct if risk_pct else 0.0
            self.db.execute(
                """UPDATE signals SET closed_at_ms=?,exit_price=?,exit_reason=?,gross_pct=?,net_pct=?,
                   result_r=?,status='CLOSED' WHERE id=?""",
                (timestamp_ms, price, reason, gross, net, result_r, row["id"]),
            )
            return {"symbol": symbol, "reason": reason, "net_pct": net, "result_r": result_r}

    def recent_signals(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(row) for row in rows]

    def recent_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(row) for row in rows]

    def record_execution(self, signal: dict[str, Any], result: dict[str, Any]) -> None:
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO executions(timestamp_ms,mode,symbol,direction,ok,signal_json,result_json) VALUES(?,?,?,?,?,?,?)",
                (
                    int(signal["timestamp_ms"]), str(result.get("mode") or "UNKNOWN"), signal["symbol"],
                    signal["direction"], int(bool(result.get("ok"))), json.dumps(signal, ensure_ascii=False),
                    json.dumps(result, ensure_ascii=False),
                ),
            )

    def recent_executions(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM executions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(row) for row in rows]

    def update_execution_payload(self, execution_id: int, result: dict[str, Any]) -> None:
        with self.lock, self.db:
            self.db.execute(
                "UPDATE executions SET result_json=? WHERE id=?",
                (json.dumps(result, ensure_ascii=False), int(execution_id)),
            )

    def recent_campaign_actions(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.db.execute(
                """SELECT id,timestamp_ms,type,symbol,direction,reason,payload_json FROM events
                   WHERE type LIKE 'CAMPAIGN_%' ORDER BY id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["payload"] = json.loads(item.pop("payload_json") or "{}")
                result.append(item)
            return result

    def pending_executions(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.db.execute(
                """SELECT e.* FROM executions e
                   LEFT JOIN execution_results r ON r.execution_id=e.id
                   WHERE e.ok=1 AND e.mode IN ('TESTNET','REAL') AND r.id IS NULL
                   ORDER BY e.id"""
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["signal"] = json.loads(item.pop("signal_json"))
                item["execution"] = json.loads(item.pop("result_json"))
                result.append(item)
            return result

    def record_execution_result(self, result: dict[str, Any]) -> None:
        with self.lock, self.db:
            self.db.execute(
                """INSERT OR IGNORE INTO execution_results(
                    execution_id,closed_at_ms,mode,symbol,direction,entry_price,exit_price,quantity,
                    realized_pnl,commission,net_pnl,net_pct,result_r,exit_reason,trades_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    result["execution_id"], result["closed_at_ms"], result["mode"], result["symbol"],
                    result["direction"], result["entry_price"], result["exit_price"], result["quantity"],
                    result["realized_pnl"], result["commission"], result["net_pnl"], result["net_pct"],
                    result["result_r"], result["exit_reason"], json.dumps(result["trades"], ensure_ascii=False),
                ),
            )

    def recent_execution_results(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM execution_results ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(row) for row in rows]

    def symbol_circuit_status(
        self, symbol: str, now_ms: int, settings: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Return a persisted cooldown after abnormal stop execution or repeated stops."""
        cfg = settings or {}
        if not bool(cfg.get("enabled", False)):
            return {"blocked": False}
        lookback_ms = int(float(cfg.get("lookback_minutes", 360)) * 60_000)
        cooldown_ms = int(float(cfg.get("cooldown_minutes", 240)) * 60_000)
        with self.lock:
            rows = self.db.execute(
                """SELECT r.*,e.result_json FROM execution_results r
                   JOIN executions e ON e.id=r.execution_id
                   WHERE r.symbol=? AND r.closed_at_ms>=?
                   ORDER BY r.closed_at_ms DESC LIMIT 20""",
                (str(symbol), int(now_ms) - lookback_ms),
            ).fetchall()
        if not rows:
            return {"blocked": False}
        latest = rows[0]
        blocked_until = int(latest["closed_at_ms"]) + cooldown_ms
        reasons: list[str] = []
        stop_slippage = 0.0
        if str(latest["exit_reason"]).upper() == "STOP":
            execution = json.loads(latest["result_json"] or "{}")
            entry = float(latest["entry_price"] or 0.0)
            exit_price = float(latest["exit_price"] or 0.0)
            initial_stop = float(execution.get("initial_stop_price") or 0.0)
            if min(entry, exit_price, initial_stop) > 0:
                long = str(latest["direction"]).upper() == "LONG"
                intended = ((entry - initial_stop) if long else (initial_stop - entry)) / entry * 100.0
                actual = ((entry - exit_price) if long else (exit_price - entry)) / entry * 100.0
                stop_slippage = max(0.0, actual - intended)
                if stop_slippage >= float(cfg.get("stop_slippage_trigger_pct", 0.50)):
                    reasons.append("abnormal_stop_slippage")
        required = max(1, int(cfg.get("consecutive_stops", 2)))
        recent = list(rows[:required])
        if len(recent) >= required and all(
            str(row["exit_reason"]).upper() == "STOP" and float(row["net_pnl"] or 0.0) < 0
            for row in recent
        ):
            reasons.append("consecutive_stops")
        blocked = bool(reasons) and int(now_ms) < blocked_until
        return {
            "blocked": blocked,
            "symbol": str(symbol),
            "reasons": reasons,
            "stop_slippage_pct": stop_slippage,
            "blocked_until_ms": blocked_until if blocked else 0,
        }

    def execution_summary(self) -> dict[str, Any]:
        with self.lock:
            rows = self.db.execute(
                """SELECT net_pnl,result_r FROM execution_results
                   WHERE exit_reason NOT LIKE 'INVALID_%'"""
            ).fetchall()
        wins = [float(row["net_pnl"]) for row in rows if float(row["net_pnl"]) > 0]
        losses = [float(row["net_pnl"]) for row in rows if float(row["net_pnl"]) < 0]
        return {
            "open": len(self.pending_executions()),
            "closed": len(rows),
            "net_pnl": sum(float(row["net_pnl"]) for row in rows),
            "expectancy_r": sum(float(row["result_r"]) for row in rows) / len(rows) if rows else 0.0,
            "win_rate_pct": len(wins) / len(rows) * 100.0 if rows else 0.0,
            "profit_factor": sum(wins) / -sum(losses) if losses else None,
        }

    def execution_summary_by_strategy(self) -> dict[str, dict[str, Any]]:
        with self.lock:
            rows = self.db.execute(
                """SELECT r.net_pnl,e.signal_json FROM execution_results r
                   JOIN executions e ON e.id=r.execution_id
                   WHERE r.exit_reason NOT LIKE 'INVALID_%'"""
            ).fetchall()
            pending = self.db.execute(
                """SELECT e.signal_json FROM executions e
                   LEFT JOIN execution_results r ON r.execution_id=e.id
                   WHERE e.ok=1 AND e.mode IN ('TESTNET','REAL') AND r.id IS NULL"""
            ).fetchall()
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            signal = json.loads(row["signal_json"] or "{}")
            name = str(signal.get("setup") or "UNKNOWN")
            group = grouped.setdefault(name, {"open": 0, "values": []})
            group["values"].append(float(row["net_pnl"] or 0.0))
        for row in pending:
            signal = json.loads(row["signal_json"] or "{}")
            name = str(signal.get("setup") or "UNKNOWN")
            grouped.setdefault(name, {"open": 0, "values": []})["open"] += 1
        result: dict[str, dict[str, Any]] = {}
        for name, group in grouped.items():
            values = group.pop("values")
            wins = [value for value in values if value > 0]
            losses = [value for value in values if value < 0]
            result[name] = {
                "open": int(group["open"]), "closed": len(values), "net_pnl": sum(values),
                "wins": len(wins), "losses": len(losses),
                "gross_profit": sum(wins), "gross_loss": -sum(losses),
                "win_rate_pct": len(wins) / len(values) * 100.0 if values else 0.0,
                "profit_factor": sum(wins) / -sum(losses) if losses else None,
            }
        return result

    def open_execution_ledger(self) -> list[dict[str, Any]]:
        pending = self.pending_executions()
        open_rows = []
        for item in pending:
            signal, execution = item["signal"], item["execution"]
            open_rows.append({
                "id": item["id"], "source": item["mode"], "status": "OPEN",
                "symbol": item["symbol"], "direction": item["direction"], "setup": signal.get("setup"),
                "campaign_id": signal.get("campaign_id"), "campaign_action": signal.get("campaign_action"),
                "opened_at_ms": item["timestamp_ms"], "entry_price": execution.get("fill_price"),
                "signal_reference_price": signal.get("entry_price"), "stop_price": execution.get("stop_price"),
                "take_price": execution.get("target_price"),
                "initial_stop_price": execution.get("initial_stop_price", execution.get("stop_price")),
                "initial_take_price": execution.get("initial_target_price", execution.get("target_price")),
                "quantity": execution.get("quantity"),
                "entry_order_id": execution.get("open_order_id"), "stop_algo_id": execution.get("stop_algo_id"),
                "take_algo_id": execution.get("take_algo_id"), "risk_pct": signal.get("risk_pct"),
                "working_type": execution.get("working_type"),
                "maker_commission_rate": execution.get("maker_commission_rate"),
                "taker_commission_rate": execution.get("taker_commission_rate"),
                "full_position_runner": bool(execution.get("full_position_runner")),
                "runner_target_disabled": bool(execution.get("runner_target_disabled")),
                "full_runner_armed": bool(execution.get("full_runner_armed")),
                "runner_armed_at_ms": execution.get("runner_armed_at_ms"),
                "profit_lock_r": execution.get("profit_lock_r"),
                "evidence": signal.get("evidence") or {},
            })
        return open_rows

    def execution_ledger(self) -> dict[str, list[dict[str, Any]]]:
        open_rows = self.open_execution_ledger()
        with self.lock:
            rows = self.db.execute(
                """SELECT r.*,e.signal_json,e.result_json FROM execution_results r
                   JOIN executions e ON e.id=r.execution_id ORDER BY r.id DESC"""
            ).fetchall()
        closed_rows = []
        for row in rows:
            item = dict(row)
            signal, execution = json.loads(item.pop("signal_json")), json.loads(item.pop("result_json"))
            try:
                fills = json.loads(item.get("trades_json") or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                fills = []
            fill_times = [int(fill.get("time") or 0) for fill in fills if int(fill.get("time") or 0) > 0]
            # Signal timestamps can precede the actual Testnet fill by minutes.
            # Duration must start at the first exchange fill, not at signal creation.
            opened_at_ms = min(fill_times) if fill_times else int(signal.get("timestamp_ms") or 0)
            closed_at_ms = int(item["closed_at_ms"])
            duration_seconds = max(0, (closed_at_ms - opened_at_ms) // 1000) if opened_at_ms else 0
            hours, remainder = divmod(duration_seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            duration_label = (
                f"{hours}h {minutes:02d}m {seconds:02d}s" if hours
                else f"{minutes}m {seconds:02d}s"
            )
            closed_rows.append({
                **item, "source": item["mode"], "status": "CLOSED", "setup": signal.get("setup"),
                "campaign_id": signal.get("campaign_id"), "campaign_action": signal.get("campaign_action"),
                "reason": item["exit_reason"], "pnl": item["net_pnl"], "pnl_net": item["net_pnl"],
                "realized_pnl": item["net_pnl"],
                "initial_stop_price": execution.get("initial_stop_price", execution.get("stop_price")),
                "initial_take_price": execution.get("initial_target_price", execution.get("target_price")),
                "stop_price": execution.get("stop_price"),
                "take_price": execution.get("target_price"), "entry_order_id": execution.get("open_order_id"),
                "stop_algo_id": execution.get("stop_algo_id"), "take_algo_id": execution.get("take_algo_id"),
                "opened_at_ms": opened_at_ms,
                "opened_at_label": datetime.fromtimestamp(opened_at_ms / 1000).strftime("%d/%m/%Y %H:%M:%S") if opened_at_ms else "-",
                "duration_seconds": duration_seconds, "duration_label": duration_label,
                "closed_at_label": datetime.fromtimestamp(item["closed_at_ms"] / 1000).strftime("%d/%m/%Y %H:%M:%S"),
            })
        return {"open": open_rows, "closed": closed_rows}

    def restored_strategy_state(self, now_ms: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
        with self.lock:
            rows = self.db.execute(
                """SELECT timestamp_ms,type,symbol,payload_json FROM events
                   WHERE type IN ('ARMED','PULLBACK','SIGNAL','EXPIRED','INVALIDATED') ORDER BY id"""
            ).fetchall()
        active: dict[str, dict[str, Any]] = {}
        cooldowns: dict[str, int] = {}
        for row in rows:
            event_type, symbol = str(row["type"]), str(row["symbol"])
            payload = json.loads(row["payload_json"] or "{}")
            if event_type == "ARMED" and isinstance(payload.get("state"), dict):
                active[symbol] = payload["state"]
            elif event_type == "PULLBACK" and symbol in active:
                if isinstance(payload.get("state"), dict):
                    active[symbol] = payload["state"]
                else:
                    active[symbol]["phase"] = "ARMED"
            elif event_type in {"SIGNAL", "EXPIRED", "INVALIDATED"}:
                active.pop(symbol, None)
                if event_type == "SIGNAL":
                    cooldowns[symbol] = int(row["timestamp_ms"])
        valid = [state for state in active.values() if int(state.get("expires_at_ms") or 0) > now_ms]
        return valid, cooldowns

    def summary(self) -> dict[str, Any]:
        with self.lock:
            rows = self.db.execute(
                "SELECT net_pct,result_r FROM signals WHERE status='CLOSED' AND source='REFERENCE'"
            ).fetchall()
        wins = [float(row["net_pct"]) for row in rows if float(row["net_pct"]) > 0]
        losses = [float(row["net_pct"]) for row in rows if float(row["net_pct"]) < 0]
        return {
            "open": self.open_count(),
            "closed": len(rows),
            "net_pct_sum": sum(float(row["net_pct"]) for row in rows),
            "expectancy_r": sum(float(row["result_r"]) for row in rows) / len(rows) if rows else 0.0,
            "win_rate_pct": len(wins) / len(rows) * 100.0 if rows else 0.0,
            "profit_factor": sum(wins) / -sum(losses) if losses else None,
        }
