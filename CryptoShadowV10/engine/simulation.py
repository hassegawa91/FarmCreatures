from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from engine.binance import MarketSnapshot


def directional_pct(direction: str, entry: float, exit_price: float) -> float:
    raw = (exit_price - entry) / entry * 100.0 if entry else 0.0
    return raw if direction == "LONG" else -raw


class SimulationLedger:
    """Persistent ledger for strategies that are forbidden from reaching the broker."""

    def __init__(self, path: Path, fee_pct_per_side: float, slippage_pct_per_side: float):
        self.path, self.fee, self.slippage = path, float(fee_pct_per_side), float(slippage_pct_per_side)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.db:
            self.db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS simulation_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy TEXT NOT NULL, symbol TEXT NOT NULL, direction TEXT NOT NULL,
                    level_key TEXT NOT NULL DEFAULT '', opened_at_ms INTEGER NOT NULL, closed_at_ms INTEGER,
                    entry_price REAL NOT NULL, stop_price REAL NOT NULL, target_price REAL,
                    exit_price REAL, exit_reason TEXT, gross_pct REAL, net_pct REAL, result_r REAL,
                    mfe_pct REAL NOT NULL DEFAULT 0, mae_pct REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'OPEN', evidence_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_simulation_open ON simulation_trades(status,strategy,symbol);
                CREATE TABLE IF NOT EXISTS simulation_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp_ms INTEGER NOT NULL,
                    strategy TEXT NOT NULL, symbol TEXT NOT NULL, event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
            """)

    def close(self) -> None:
        with self.lock:
            self.db.close()

    def reset(self) -> dict[str, int]:
        tables = ("simulation_events", "simulation_trades")
        with self.lock, self.db:
            counts = {
                table: int(self.db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
                for table in tables
            }
            for table in tables:
                self.db.execute(f'DELETE FROM "{table}"')
            self.db.execute("DELETE FROM sqlite_sequence WHERE name IN (?,?)", tables)
        return counts

    def event(self, timestamp_ms: int, strategy: str, symbol: str, event_type: str, payload: dict[str, Any]) -> None:
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO simulation_events(timestamp_ms,strategy,symbol,event_type,payload_json) VALUES(?,?,?,?,?)",
                (int(timestamp_ms), strategy, symbol, event_type, json.dumps(payload, ensure_ascii=False)),
            )

    def open_trade(self, strategy: str, snapshot: MarketSnapshot, direction: str, entry: float,
                   stop: float, target: float | None, evidence: dict[str, Any], level_key: str = "",
                   apply_entry_slippage: bool = True) -> bool:
        with self.lock, self.db:
            if self.db.execute(
                "SELECT 1 FROM simulation_trades WHERE status='OPEN' AND strategy=? AND symbol=? AND level_key=?",
                (strategy, snapshot.symbol, level_key),
            ).fetchone():
                return False
            adjusted = (
                entry * (1 + self.slippage / 100) if direction == "LONG"
                else entry * (1 - self.slippage / 100)
            ) if apply_entry_slippage else float(entry)
            self.db.execute(
                """INSERT INTO simulation_trades(strategy,symbol,direction,level_key,opened_at_ms,
                   entry_price,stop_price,target_price,evidence_json) VALUES(?,?,?,?,?,?,?,?,?)""",
                (strategy, snapshot.symbol, direction, level_key, int(snapshot.timestamp_ms), adjusted,
                 float(stop), float(target) if target else None, json.dumps(evidence, ensure_ascii=False)),
            )
            self.event(snapshot.timestamp_ms, strategy, snapshot.symbol, "OPEN", {
                "direction": direction, "entry": adjusted, "stop": stop, "target": target, "level_key": level_key,
            })
            return True

    def open_rows(self, strategy: str | None = None, symbol: str | None = None) -> list[dict[str, Any]]:
        query, args = "SELECT * FROM simulation_trades WHERE status='OPEN'", []
        if strategy:
            query, args = query + " AND strategy=?", [strategy]
        if symbol:
            query, args = query + " AND symbol=?", args + [symbol]
        with self.lock:
            rows = [dict(row) for row in self.db.execute(query, args).fetchall()]
        for row in rows:
            row["evidence"] = json.loads(row.get("evidence_json") or "{}")
        return rows

    def open_symbols(self) -> list[str]:
        with self.lock:
            return [row[0] for row in self.db.execute(
                "SELECT DISTINCT symbol FROM simulation_trades WHERE status='OPEN'"
            ).fetchall()]

    def has_trade(self, strategy: str, symbol: str, level_key: str) -> bool:
        with self.lock:
            return self.db.execute(
                "SELECT 1 FROM simulation_trades WHERE strategy=? AND symbol=? AND level_key=? LIMIT 1",
                (strategy, symbol, level_key),
            ).fetchone() is not None

    def update_extremes(self, trade_id: int, mfe_pct: float, mae_pct: float) -> None:
        with self.lock, self.db:
            self.db.execute(
                "UPDATE simulation_trades SET mfe_pct=MAX(mfe_pct,?),mae_pct=MIN(mae_pct,?) WHERE id=?",
                (float(mfe_pct), float(mae_pct), int(trade_id)),
            )

    def close_trade(self, row: dict[str, Any], exit_price: float, timestamp_ms: int, reason: str) -> None:
        direction = str(row["direction"])
        adjusted = exit_price * (1 - self.slippage / 100) if direction == "LONG" else exit_price * (1 + self.slippage / 100)
        gross = directional_pct(direction, float(row["entry_price"]), adjusted)
        net = gross - 2 * self.fee
        risk = abs(float(row["entry_price"]) - float(row["stop_price"])) / float(row["entry_price"]) * 100
        result_r = net / risk if risk else 0.0
        with self.lock, self.db:
            self.db.execute(
                """UPDATE simulation_trades SET status='CLOSED',closed_at_ms=?,exit_price=?,exit_reason=?,
                   gross_pct=?,net_pct=?,result_r=? WHERE id=?""",
                (int(timestamp_ms), adjusted, reason, gross, net, result_r, int(row["id"])),
            )
            self.event(timestamp_ms, str(row["strategy"]), str(row["symbol"]), "CLOSE", {
                "exit": adjusted, "reason": reason, "net_pct": net, "result_r": result_r,
            })

    def summary(self) -> dict[str, Any]:
        names = {"TREND_EMA_OI_FUNDING", "DYNAMIC_LATERAL_GRID", "DYNAMIC_LATERAL_GRID_V2_COST_AWARE"}
        with self.lock:
            rows = self.db.execute(
                "SELECT strategy,status,net_pct,result_r,evidence_json FROM simulation_trades"
            ).fetchall()
        names.update(str(row["strategy"]) for row in rows)
        result = {}
        for name in sorted(names):
            selected = [row for row in rows if row["strategy"] == name]
            closed = [row for row in selected if row["status"] == "CLOSED"]
            wins = [float(row["net_pct"]) for row in closed if float(row["net_pct"]) > 0]
            losses = [float(row["net_pct"]) for row in closed if float(row["net_pct"]) < 0]
            net_pnl_usdt = 0.0
            for row in closed:
                evidence = json.loads(row["evidence_json"] or "{}")
                notional = float(evidence.get("notional_usdt") or 0.0)
                net_pnl_usdt += float(row["net_pct"] or 0.0) / 100.0 * notional
            result[name] = {
                "open": sum(row["status"] == "OPEN" for row in selected), "closed": len(closed),
                "net_pct_sum": sum(float(row["net_pct"]) for row in closed),
                "expectancy_r": sum(float(row["result_r"]) for row in closed) / len(closed) if closed else 0.0,
                "wins": len(wins),
                "gross_profit": sum(wins),
                "gross_loss": -sum(losses),
                "win_rate_pct": len(wins) / len(closed) * 100 if closed else 0.0,
                "profit_factor": sum(wins) / -sum(losses) if losses else None,
                "net_pnl_usdt": net_pnl_usdt,
            }
        staged = [row for row in rows if str(row["strategy"]).startswith("STAGED_FADE_")]
        staged_closed = [row for row in staged if row["status"] == "CLOSED"]
        staged_wins = [float(row["net_pct"]) for row in staged_closed if float(row["net_pct"]) > 0]
        staged_losses = [float(row["net_pct"]) for row in staged_closed if float(row["net_pct"]) < 0]
        staged_pnl = 0.0
        for row in staged_closed:
            evidence = json.loads(row["evidence_json"] or "{}")
            staged_pnl += float(row["net_pct"] or 0.0) / 100.0 * float(
                evidence.get("notional_usdt") or 0.0
            )
        result["STAGED_FADE_POLICY"] = {
            "open": sum(row["status"] == "OPEN" for row in staged),
            "closed": len(staged_closed),
            "net_pct_sum": sum(float(row["net_pct"] or 0.0) for row in staged_closed),
            "expectancy_r": (
                sum(float(row["result_r"] or 0.0) for row in staged_closed) / len(staged_closed)
                if staged_closed else 0.0
            ),
            "wins": len(staged_wins),
            "gross_profit": sum(staged_wins),
            "gross_loss": -sum(staged_losses),
            "win_rate_pct": len(staged_wins) / len(staged_closed) * 100 if staged_closed else 0.0,
            "profit_factor": sum(staged_wins) / -sum(staged_losses) if staged_losses else None,
            "net_pnl_usdt": staged_pnl,
        }
        return result

    def recent(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM simulation_trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["evidence"] = json.loads(item.pop("evidence_json") or "{}")
            notional = float(item["evidence"].get("notional_usdt") or 0.0)
            margin = float(item["evidence"].get("margin_usdt") or 0.0)
            if item.get("net_pct") is not None and notional > 0:
                item["estimated_net_pnl_usdt"] = float(item["net_pct"]) / 100.0 * notional
                item["estimated_roe_pct"] = (
                    item["estimated_net_pnl_usdt"] / margin * 100.0 if margin > 0 else None
                )
            result.append(item)
        return result


class ParallelStrategyLab:
    TREND = "TREND_EMA_OI_FUNDING"
    GRID_BASELINE = "DYNAMIC_LATERAL_GRID"
    GRID = "DYNAMIC_LATERAL_GRID_V2_COST_AWARE"
    VOLATILITY_FADE_SCALP = "VOLATILITY_EXHAUSTION_FADE_SCALP_V1"
    LOGICAL_PREFIX = "NATIVE_TP_RUNNER::"
    LEVERAGED_HALF_PREFIX = "LEV20_HALF_GEOMETRY::"
    DUMP_LOGICAL = LOGICAL_PREFIX + "DUMP_EXHAUSTION_RECLAIM_V1"
    STAGED_PROBE_PREFIX = "STAGED_FADE_PROBE::"
    STAGED_ADD_PREFIX = "STAGED_FADE_ADD::"

    def __init__(self, settings: dict[str, Any], path: Path):
        self.settings, self.enabled = settings, bool(settings.get("enabled", True))
        self.ledger = SimulationLedger(path, settings.get("fee_pct_per_side", 0.05), settings.get("slippage_pct_per_side", 0.02))
        self.grid_sessions: dict[str, dict[str, Any]] = {}
        self.last_trend_entry: dict[tuple[str, str], int] = {}
        self.last_grid_fill_candle: dict[tuple[str, str], int] = {}
        self.grid_observations: dict[str, dict[str, int]] = {}
        self.grid_cooldowns: dict[str, int] = {}
        self.last_scalp_entry_candle: dict[str, int] = {}
        self.leveraged_live_prices: dict[str, float] = {}

    def close(self) -> None:
        self.ledger.close()

    def reset(self) -> dict[str, int]:
        counts = self.ledger.reset()
        self.grid_sessions.clear()
        self.last_trend_entry.clear()
        self.last_grid_fill_candle.clear()
        self.grid_observations.clear()
        self.grid_cooldowns.clear()
        self.last_scalp_entry_candle.clear()
        self.leveraged_live_prices.clear()
        return counts

    def open_symbols(self) -> list[str]:
        return self.ledger.open_symbols()

    def _manage_open(self, snapshot: MarketSnapshot) -> None:
        self._open_staged_add(snapshot)
        for row in self.ledger.open_rows(symbol=snapshot.symbol):
            # The leveraged profile is managed exclusively by the 2-second
            # production-price stream received from Real Shadow.
            if str(row["strategy"]).startswith(self.LEVERAGED_HALF_PREFIX):
                continue
            entry, direction = float(row["entry_price"]), str(row["direction"])
            evidence = row.get("evidence") or {}
            if (
                (
                    (
                        str(row["strategy"]).startswith(self.LOGICAL_PREFIX)
                        or str(row["strategy"]).startswith(self.STAGED_PROBE_PREFIX)
                        or str(row["strategy"]).startswith(self.STAGED_ADD_PREFIX)
                        or str(row["strategy"]).startswith(self.LEVERAGED_HALF_PREFIX)
                    )
                    or bool(evidence.get("defer_same_candle_management"))
                )
                and int(snapshot.candle_open_time_ms) <= int(
                    evidence.get("source_candle_open_time_ms") or 0
                )
            ):
                # The candle OHLC contains price action from before the intra-candle signal.
                # Start evaluating only on a later 5m candle to avoid false stops/targets.
                continue
            if direction == "LONG":
                mfe, mae = directional_pct(direction, entry, snapshot.candle_high), directional_pct(direction, entry, snapshot.candle_low)
                stop_hit, target_hit = snapshot.candle_low <= row["stop_price"], row["target_price"] and snapshot.candle_high >= row["target_price"]
            else:
                mfe, mae = directional_pct(direction, entry, snapshot.candle_low), directional_pct(direction, entry, snapshot.candle_high)
                stop_hit, target_hit = snapshot.candle_high >= row["stop_price"], row["target_price"] and snapshot.candle_low <= row["target_price"]
            self.ledger.update_extremes(row["id"], mfe, mae)
            reason, price = ("STOP", float(row["stop_price"])) if stop_hit else (("TARGET", float(row["target_price"])) if target_hit else ("", 0.0))
            if not reason and (
                str(row["strategy"]).startswith(self.LOGICAL_PREFIX)
                or str(row["strategy"]).startswith(self.STAGED_PROBE_PREFIX)
                or str(row["strategy"]).startswith(self.STAGED_ADD_PREFIX)
                or str(row["strategy"]).startswith(self.LEVERAGED_HALF_PREFIX)
            ):
                cfg = self.settings.get("logical_exit") or self.settings.get("dump_logical_exit") or {}
                peak_pct = max(float(row.get("mfe_pct") or 0.0), float(mfe))
                activation_pct = float(evidence.get("native_target_pct") or 0.0)
                if peak_pct >= activation_pct:
                    current_pct = directional_pct(direction, entry, float(snapshot.price))
                    volatility_giveback = float(snapshot.atr14_pct) * float(
                        cfg.get("atr_giveback_multiple", 1.25)
                    )
                    geometry_scale = float(evidence.get("geometry_scale") or 1.0)
                    giveback_pct = max(
                        float(cfg.get("min_giveback_pct", 0.65)) * geometry_scale,
                        min(
                            float(cfg.get("max_giveback_pct", 1.40)) * geometry_scale,
                            volatility_giveback * geometry_scale,
                        ),
                    )
                    logical_floor_pct = max(
                        activation_pct,
                        peak_pct - giveback_pct,
                    )
                    if current_pct <= logical_floor_pct:
                        reason, price = "LOGICAL_TRAIL", float(snapshot.price)
            if not reason and row["strategy"] == self.TREND:
                crossed = snapshot.candle_close < snapshot.ema21 if direction == "LONG" else snapshot.candle_close > snapshot.ema21
                if crossed:
                    reason, price = "EMA21_CROSS", snapshot.candle_close
            if reason:
                self.ledger.close_trade(row, price, snapshot.timestamp_ms, reason)

    def open_staged_fade(self, signal: dict[str, Any], snapshot: MarketSnapshot) -> bool:
        cfg = self.settings.get("staged_fade") or {}
        if not self.enabled or not bool(cfg.get("enabled", False)):
            return False
        setup = str(signal.get("setup") or "")
        if setup != self.VOLATILITY_FADE_SCALP:
            return False
        direction = str(signal["direction"])
        fraction = float(
            cfg.get("long_probe_fraction", 0.25)
            if direction == "LONG" else cfg.get("short_probe_fraction", 0.10)
        )
        entry = float(signal["entry_price"])
        stop = float(signal["stop_price"])
        target = float(signal["target_price"])
        margin = float(cfg.get("margin_usdt", 50.0))
        leverage = int(cfg.get("leverage", 10))
        key = str(signal.get("timestamp_ms") or snapshot.timestamp_ms)
        evidence = {
            **(signal.get("evidence") or {}),
            "source_setup": setup,
            "source_signal_timestamp_ms": signal.get("timestamp_ms"),
            "source_candle_open_time_ms": int(snapshot.candle_open_time_ms),
            "native_target_price": target,
            "native_target_pct": directional_pct(direction, entry, target),
            "original_entry_price": entry,
            "original_stop_price": stop,
            "original_target_price": target,
            "add_trigger_r": float(cfg.get("long_add_trigger_r", 0.20)),
            "leg": "PROBE", "position_fraction": fraction,
            "margin_usdt": margin * fraction,
            "leverage": leverage,
            "notional_usdt": margin * leverage * fraction,
            "simulation_only": True,
        }
        return self.ledger.open_trade(
            self.STAGED_PROBE_PREFIX + setup, snapshot, direction, entry, stop, None,
            evidence, key,
        )

    def _open_staged_add(self, snapshot: MarketSnapshot) -> None:
        cfg = self.settings.get("staged_fade") or {}
        if not bool(cfg.get("enabled", False)) or not bool(cfg.get("long_add_enabled", True)):
            return
        for probe in self.ledger.open_rows(symbol=snapshot.symbol):
            if not str(probe["strategy"]).startswith(self.STAGED_PROBE_PREFIX):
                continue
            if str(probe["direction"]) != "LONG":
                continue
            evidence = probe.get("evidence") or {}
            original_entry = float(evidence.get("original_entry_price") or probe["entry_price"])
            original_stop = float(evidence.get("original_stop_price") or probe["stop_price"])
            original_target = float(evidence.get("original_target_price") or 0.0)
            risk_pct = abs(original_entry - original_stop) / original_entry * 100.0
            trigger_r = float(evidence.get("add_trigger_r") or 0.20)
            current_r = (
                directional_pct("LONG", original_entry, float(snapshot.price)) / risk_pct
                if risk_pct > 0 else 0.0
            )
            if current_r < trigger_r:
                continue
            setup = str(evidence.get("source_setup") or self.VOLATILITY_FADE_SCALP)
            strategy = self.STAGED_ADD_PREFIX + setup
            key = str(probe["level_key"])
            if self.ledger.has_trade(strategy, snapshot.symbol, key):
                continue
            fraction = 1.0 - float(cfg.get("long_probe_fraction", 0.25))
            margin = float(cfg.get("margin_usdt", 50.0))
            leverage = int(cfg.get("leverage", 10))
            add_entry = float(snapshot.price)
            add_evidence = {
                **evidence,
                "source_candle_open_time_ms": int(snapshot.candle_open_time_ms),
                "native_target_pct": directional_pct("LONG", add_entry, original_target),
                "leg": "ADD", "position_fraction": fraction,
                "triggered_at_r": current_r,
                "margin_usdt": margin * fraction,
                "notional_usdt": margin * leverage * fraction,
            }
            self.ledger.open_trade(
                strategy, snapshot, "LONG", add_entry, original_stop, None,
                add_evidence, key,
            )

    def open_native_target_runner(self, signal: dict[str, Any], snapshot: MarketSnapshot) -> bool:
        """Turn each setup's own target into a protected floor and let excess profit run."""
        if not self.enabled:
            return False
        cfg = self.settings.get("logical_exit") or self.settings.get("dump_logical_exit") or {}
        if not bool(cfg.get("enabled", False)):
            return False
        setup = str(signal.get("setup") or "")
        if not setup:
            return False
        entry = float(signal["entry_price"])
        direction = str(signal["direction"])
        stop = float(signal["stop_price"])
        target = float(signal["target_price"])
        activation_pct = directional_pct(direction, entry, target)
        if activation_pct <= 0:
            return False
        evidence = {
            **(signal.get("evidence") or {}),
            "source_setup": setup,
            "source_signal_timestamp_ms": signal.get("timestamp_ms"),
            "source_candle_open_time_ms": int(snapshot.candle_open_time_ms),
            "exit_model": "SETUP_NATIVE_TARGET_AS_FLOOR__ATR_PEAK_TRAIL",
            "native_target_price": target,
            "native_target_pct": activation_pct,
            "simulation_only": True,
        }
        return self.ledger.open_trade(
            self.LOGICAL_PREFIX + setup, snapshot, direction, entry, stop, None, evidence,
            str(signal.get("timestamp_ms") or snapshot.timestamp_ms),
        )

    def open_leveraged_half_geometry(
        self, signal: dict[str, Any], snapshot: MarketSnapshot,
        shadow_trade: dict[str, Any] | None = None,
    ) -> bool:
        """Mirror an accepted Real Shadow fill at 50 USDT x 20x and half geometry."""
        cfg = self.settings.get("leveraged_half_geometry") or {}
        if not self.enabled or not bool(cfg.get("enabled", False)):
            return False
        setup = str(signal.get("setup") or "")
        if not setup:
            return False
        # This profile is intentionally downstream from Shadow. Signals rejected
        # by its real spread/depth/drift checks must never reach this ledger.
        if not shadow_trade:
            return False
        if any(
            str(row["strategy"]).startswith(self.LEVERAGED_HALF_PREFIX)
            for row in self.ledger.open_rows(symbol=snapshot.symbol)
        ):
            return False
        entry = float(shadow_trade["entry_price"])
        direction = str(signal["direction"])
        scale = float(cfg.get("geometry_scale", 0.50))
        source_stop = float(shadow_trade["initial_stop_price"])
        source_target = float(shadow_trade["target_price"])
        stop = entry + (source_stop - entry) * scale
        target = entry + (source_target - entry) * scale
        activation_pct = directional_pct(direction, entry, target)
        evidence = {
            **(signal.get("evidence") or {}),
            "source_setup": setup,
            "source_signal_timestamp_ms": signal.get("timestamp_ms"),
            "source_shadow_trade_id": shadow_trade.get("id"),
            "source": "REAL_SHADOW_ACCEPTED_FILL",
            "source_candle_open_time_ms": int(snapshot.candle_open_time_ms),
            "native_target_price": target,
            "native_target_pct": activation_pct,
            "geometry_scale": scale,
            "margin_usdt": float(cfg.get("margin_usdt", 50.0)),
            "leverage": int(cfg.get("leverage", 20)),
            "notional_usdt": float(cfg.get("margin_usdt", 50.0)) * int(cfg.get("leverage", 20)),
            "exit_model": "HALF_TP_SL__HALF_ATR_RUNNER",
            "simulation_only": True,
        }
        return self.ledger.open_trade(
            self.LEVERAGED_HALF_PREFIX + setup,
            snapshot, direction, entry, stop, None, evidence,
            str(shadow_trade.get("id") or signal.get("timestamp_ms") or snapshot.timestamp_ms),
            apply_entry_slippage=False,
        )

    def on_real_shadow_open(
        self, signal: dict[str, Any], shadow_trade: dict[str, Any], snapshot: MarketSnapshot,
    ) -> bool:
        return self.open_leveraged_half_geometry(signal, snapshot, shadow_trade)

    def leveraged_open_symbols(self) -> set[str]:
        return {
            str(row["symbol"]) for row in self.ledger.open_rows()
            if str(row["strategy"]).startswith(self.LEVERAGED_HALF_PREFIX)
        }

    def on_real_shadow_price(self, symbol: str, price: float, timestamp_ms: int) -> None:
        """Manage the 20x variant from the same production-price ticks as Shadow."""
        self.leveraged_live_prices[str(symbol)] = float(price)
        cfg = self.settings.get("logical_exit") or self.settings.get("dump_logical_exit") or {}
        for row in self.ledger.open_rows(symbol=symbol):
            if not str(row["strategy"]).startswith(self.LEVERAGED_HALF_PREFIX):
                continue
            entry, direction = float(row["entry_price"]), str(row["direction"])
            current_pct = directional_pct(direction, entry, float(price))
            peak_pct = max(float(row.get("mfe_pct") or 0.0), current_pct)
            adverse_pct = min(float(row.get("mae_pct") or 0.0), current_pct)
            self.ledger.update_extremes(row["id"], peak_pct, adverse_pct)
            stop_hit = (
                float(price) <= float(row["stop_price"])
                if direction == "LONG" else float(price) >= float(row["stop_price"])
            )
            if stop_hit:
                self.ledger.close_trade(row, float(price), int(timestamp_ms), "SHADOW_PRICE_STOP")
                continue
            evidence = row.get("evidence") or {}
            activation = float(evidence.get("native_target_pct") or 0.0)
            if peak_pct < activation:
                continue
            scale = float(evidence.get("geometry_scale") or 0.5)
            atr_pct = float(((evidence.get("entry") or {}).get("atr14_pct")) or 0.0)
            giveback = max(
                float(cfg.get("min_giveback_pct", 0.65)) * scale,
                min(
                    float(cfg.get("max_giveback_pct", 1.40)) * scale,
                    atr_pct * float(cfg.get("atr_giveback_multiple", 1.25)) * scale,
                ),
            )
            floor_pct = max(activation, peak_pct - giveback)
            if current_pct <= floor_pct:
                self.ledger.close_trade(row, float(price), int(timestamp_ms), "SHADOW_PRICE_RUNNER")

    def on_strategy_signal(self, signal: dict[str, Any], snapshot: MarketSnapshot) -> bool:
        """Consume live strategy signals as logical-exit events, then mirror new Dump entries."""
        if not self.enabled:
            return False
        for row in self.ledger.open_rows(symbol=snapshot.symbol):
            if not str(row["strategy"]).startswith(self.LOGICAL_PREFIX):
                continue
            if str(row["direction"]) == str(signal.get("direction") or ""):
                continue
            peak = float(row.get("mfe_pct") or 0.0)
            activation = float((row.get("evidence") or {}).get("native_target_pct") or 0.0)
            if peak >= activation:
                self.ledger.close_trade(
                    row, float(snapshot.price), int(snapshot.timestamp_ms), "LOGICAL_OPPOSITE_SIGNAL",
                )
        opened = self.open_native_target_runner(signal, snapshot)
        self.open_staged_fade(signal, snapshot)
        return opened

    def _trend(self, snapshot: MarketSnapshot) -> None:
        cfg = self.settings.get("trend") or {}
        if not bool(cfg.get("enabled", True)):
            return
        if snapshot.adx14 < cfg.get("min_adx", 20.0) or snapshot.oi_change_15m_pct < cfg.get("min_oi_15m_pct", 2.0):
            return
        funding_limit, direction = float(cfg.get("max_abs_funding_pct", 0.05)), ""
        if snapshot.candle_close > snapshot.ema21 and snapshot.ema21_slope_pct > 0 and -funding_limit <= snapshot.funding_rate_pct <= funding_limit:
            direction = "LONG"
        elif snapshot.candle_close < snapshot.ema21 and snapshot.ema21_slope_pct < 0 and snapshot.funding_rate_pct <= 0:
            direction = "SHORT"
        if not direction or self.ledger.open_rows(self.TREND, snapshot.symbol):
            return
        key, cooldown = (snapshot.symbol, direction), int(cfg.get("cooldown_minutes", 60) * 60_000)
        if key in self.last_trend_entry and snapshot.timestamp_ms - self.last_trend_entry[key] < cooldown:
            return
        entry = snapshot.candle_close
        distance = max(snapshot.atr14 * cfg.get("stop_atr_multiple", 1.5), entry * cfg.get("min_stop_pct", 0.5) / 100)
        stop = entry - distance if direction == "LONG" else entry + distance
        target = entry + distance * cfg.get("target_r", 2.0) if direction == "LONG" else entry - distance * cfg.get("target_r", 2.0)
        if self.ledger.open_trade(self.TREND, snapshot, direction, entry, stop, target,
                                  {**snapshot.to_dict(), "rule": "EMA21+OI+funding", "simulation_only": True}):
            self.last_trend_entry[key] = snapshot.timestamp_ms

    def _grid(self, snapshot: MarketSnapshot) -> None:
        cfg, session = self.settings.get("grid") or {}, self.grid_sessions.get(snapshot.symbol)
        if not bool(cfg.get("enabled", True)):
            return
        now_ms = int(snapshot.timestamp_ms)
        valid = (0 < snapshot.adx14 < cfg.get("max_adx", 20.0)
                 and snapshot.atr14_pct <= cfg.get("max_atr_pct", 1.2)
                 and cfg.get("min_range_width_pct", 0.6) <= snapshot.range_width_pct <= cfg.get("max_range_width_pct", 2.5)
                 and snapshot.volume_ratio <= cfg.get("max_volume_ratio", 1.2)
                 and abs(snapshot.oi_change_15m_pct) <= cfg.get("max_abs_oi_15m_pct", 0.75))
        if session and (
            snapshot.candle_close < session["global_low"]
            or snapshot.candle_close > session["global_high"]
            or snapshot.adx14 >= cfg.get("breakout_adx", 25.0)
            or now_ms >= session["expires_at_ms"]
        ):
            reason = "BREAKOUT" if snapshot.candle_close < session["global_low"] or snapshot.candle_close > session["global_high"] else "REGIME_EXIT"
            self.ledger.event(now_ms, self.GRID, snapshot.symbol, "GRID_INVALIDATED", {"reason": reason, **session})
            self.grid_sessions.pop(snapshot.symbol, None)
            self.grid_cooldowns[snapshot.symbol] = now_ms + int(cfg.get("rearm_cooldown_minutes", 30) * 60_000)
            session = None
        observation = self.grid_observations.setdefault(snapshot.symbol, {"count": 0, "candle_ms": 0})
        if snapshot.candle_open_time_ms > observation["candle_ms"]:
            observation["count"] = observation["count"] + 1 if valid else 0
            observation["candle_ms"] = snapshot.candle_open_time_ms
        if (
            session is None and valid and snapshot.prior_high > snapshot.prior_low > 0
            and observation["count"] >= int(cfg.get("min_stable_observations", 3))
            and now_ms >= self.grid_cooldowns.get(snapshot.symbol, 0)
        ):
            if self.ledger.open_rows(self.GRID, snapshot.symbol):
                return
            low, high = snapshot.prior_low, snapshot.prior_high
            center = (high + low) / 2
            configured_cost = 2.0 * (self.ledger.fee + self.ledger.slippage)
            minimum_spacing_pct = max(
                float(cfg.get("min_spacing_pct", 0.30)),
                configured_cost + float(cfg.get("min_net_target_pct", 0.15)),
            )
            spacing = center * minimum_spacing_pct / 100.0
            maximum_levels = max(0, int((high - low) / (2.0 * spacing)))
            levels = min(int(cfg.get("levels_each_side", 4)), maximum_levels)
            if levels < int(cfg.get("min_levels_each_side", 1)):
                self.ledger.event(now_ms, self.GRID, snapshot.symbol, "GRID_REJECTED", {
                    "reason": "range_does_not_pay_costs", "range_width_pct": snapshot.range_width_pct,
                    "required_spacing_pct": minimum_spacing_pct,
                })
                self.grid_cooldowns[snapshot.symbol] = now_ms + int(cfg.get("recheck_minutes", 15) * 60_000)
                return
            outer = spacing * (levels + float(cfg.get("global_stop_buffer_levels", 0.75)))
            session = {
                "created_candle_ms": snapshot.candle_open_time_ms, "created_at_ms": now_ms,
                "expires_at_ms": now_ms + int(cfg.get("session_ttl_minutes", 120) * 60_000),
                "spacing": spacing, "spacing_pct": minimum_spacing_pct, "levels": levels,
                "global_low": center - outer, "global_high": center + outer,
                "buy_levels": [center - spacing * i for i in range(1, levels + 1)],
                "sell_levels": [center + spacing * i for i in range(1, levels + 1)],
                "last_price": snapshot.candle_close,
                "last_candle_ms": snapshot.candle_open_time_ms,
            }
            self.grid_sessions[snapshot.symbol] = session
            self.ledger.event(snapshot.timestamp_ms, self.GRID, snapshot.symbol, "GRID_CREATED", session)
        if not session or snapshot.candle_open_time_ms <= session["last_candle_ms"] or len(self.ledger.open_rows(self.GRID)) >= cfg.get("max_open_trades", 12):
            return
        previous_price = float(session["last_price"])
        session["last_price"] = snapshot.candle_close
        session["last_candle_ms"] = snapshot.candle_open_time_ms
        for side, direction, levels in (("BUY", "LONG", session["buy_levels"]), ("SELL", "SHORT", session["sell_levels"])):
            direction_count = sum(
                row["direction"] == direction for row in self.ledger.open_rows(self.GRID, snapshot.symbol)
            )
            if direction_count >= int(cfg.get("max_open_per_direction_per_symbol", 2)):
                continue
            for index, level in enumerate(levels):
                touched = (
                    previous_price > level and snapshot.candle_low <= level
                    if direction == "LONG"
                    else previous_price < level and snapshot.candle_high >= level
                )
                level_key = f"{session['created_candle_ms']}:{side}:{index}"
                fill_key = (snapshot.symbol, level_key)
                if touched and self.last_grid_fill_candle.get(fill_key) != snapshot.candle_open_time_ms:
                    stop_levels = float(cfg.get("max_stop_distance_levels", 1.50))
                    stop = (
                        max(session["global_low"], level - session["spacing"] * stop_levels)
                        if direction == "LONG"
                        else min(session["global_high"], level + session["spacing"] * stop_levels)
                    )
                    target = level + session["spacing"] if direction == "LONG" else level - session["spacing"]
                    opened = self.ledger.open_trade(
                        self.GRID, snapshot, direction, level, stop, target,
                        {**snapshot.to_dict(), "grid": session, "simulation_only": True}, level_key,
                    )
                    if opened:
                        self.last_grid_fill_candle[fill_key] = snapshot.candle_open_time_ms

    def _volatility_fade_scalp(self, snapshot: MarketSnapshot) -> None:
        """Fade short bursts only after directional exhaustion and visible rejection.

        This is deliberately simulation-only. It produces a high-frequency sample without
        allowing an unvalidated candle-colour rule to reach the broker.
        """
        cfg = self.settings.get("volatility_fade_scalp") or {}
        if not bool(cfg.get("enabled", False)):
            return
        candle_ms = int(snapshot.candle_open_time_ms)
        if self.last_scalp_entry_candle.get(snapshot.symbol) == candle_ms:
            return
        if len(self.ledger.open_rows(self.VOLATILITY_FADE_SCALP)) >= int(
            cfg.get("max_open_trades", 10)
        ):
            return
        if self.ledger.open_rows(self.VOLATILITY_FADE_SCALP, snapshot.symbol):
            return
        opened = float(snapshot.candle_open)
        closed = float(snapshot.candle_close)
        high = float(snapshot.candle_high)
        low = float(snapshot.candle_low)
        if opened <= 0 or high <= low or closed == opened:
            return
        body_pct = abs(closed / opened - 1.0) * 100.0
        if body_pct < float(cfg.get("min_body_pct", 0.40)):
            return
        if int(snapshot.directional_candle_count) < int(cfg.get("min_directional_candles", 2)):
            return
        green = closed > opened
        rejection = (high - closed) / (high - low) if green else (closed - low) / (high - low)
        if rejection < float(cfg.get("min_rejection_wick_fraction", 0.15)):
            return
        # Exhaustion fade: a green burst is faded SHORT; a red burst is faded LONG.
        direction = "SHORT" if green else "LONG"
        entry = float(snapshot.price)
        rejection_break_pct = float(cfg.get("max_rejection_break_pct", 0.10))
        rejection_structure_intact = (
            entry <= high * (1.0 + rejection_break_pct / 100.0)
            if direction == "SHORT" else
            entry >= low * (1.0 - rejection_break_pct / 100.0)
        )
        if not rejection_structure_intact:
            return
        stop_pct = float(cfg.get("stop_pct", 2.00))
        target_pct = float(cfg.get("target_pct", 0.80))
        stop = entry * (1.0 + stop_pct / 100.0) if direction == "SHORT" else entry * (
            1.0 - stop_pct / 100.0
        )
        target = entry * (1.0 - target_pct / 100.0) if direction == "SHORT" else entry * (
            1.0 + target_pct / 100.0
        )
        evidence = {
            **snapshot.to_dict(),
            "rule": "TWO_CANDLE_EXHAUSTION_WITH_REJECTION_WICK",
            "body_pct": body_pct,
            "rejection_wick_fraction": rejection,
            "source_candle_open_time_ms": candle_ms,
            "defer_same_candle_management": True,
            "simulation_only": True,
        }
        if self.ledger.open_trade(
            self.VOLATILITY_FADE_SCALP, snapshot, direction, entry, stop, target,
            evidence, str(candle_ms),
        ):
            self.last_scalp_entry_candle[snapshot.symbol] = candle_ms

    def process(self, snapshot: MarketSnapshot) -> None:
        if self.enabled:
            self._manage_open(snapshot)
            self._trend(snapshot)
            self._grid(snapshot)
            self._volatility_fade_scalp(snapshot)

    def status(self) -> dict[str, Any]:
        recent = self.ledger.recent(200)
        for row in recent:
            if row["status"] != "OPEN" or not str(row["strategy"]).startswith(self.LEVERAGED_HALF_PREFIX):
                continue
            price = float(self.leveraged_live_prices.get(str(row["symbol"])) or row["entry_price"])
            evidence = row.get("evidence") or {}
            raw = directional_pct(str(row["direction"]), float(row["entry_price"]), price)
            net_pct = raw - 2.0 * self.ledger.fee
            notional = float(evidence.get("notional_usdt") or 1000.0)
            margin = float(evidence.get("margin_usdt") or 50.0)
            row["current_price"] = price
            row["estimated_open_pnl_usdt"] = net_pct / 100.0 * notional
            row["estimated_open_roe_pct"] = row["estimated_open_pnl_usdt"] / margin * 100.0
        return {"enabled": self.enabled, "database": str(self.ledger.path),
                "strategies": ["NATIVE_TP_RUNNER::<SETUP>", "STAGED_FADE_POLICY", "LEV20_HALF_GEOMETRY::<SETUP>", self.TREND, self.GRID_BASELINE,
                               self.GRID, self.VOLATILITY_FADE_SCALP], "active_grid_sessions": len(self.grid_sessions),
                "summary": self.ledger.summary(), "recent_trades": recent}
