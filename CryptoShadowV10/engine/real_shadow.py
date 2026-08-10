from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from engine.binance import BinancePublicClient, MarketSnapshot, number
from engine.campaign import CampaignPolicy


class RealMarketShadow:
    """Paper account driven only by Binance production prices and order-book depth."""

    def __init__(
        self, settings: dict[str, Any], execution_settings: dict[str, Any],
        campaign: CampaignPolicy, client: BinancePublicClient, path: Path,
        leveraged_mirror: Any | None = None,
    ):
        self.settings = settings or {}
        self.execution_settings = execution_settings
        self.campaign = campaign
        self.client = client
        self.enabled = bool(self.settings.get("enabled", False))
        self.leveraged_mirror = leveraged_mirror
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._create_schema()

    def _create_schema(self) -> None:
        with self.db:
            self.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS real_shadow_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    campaign_id TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    opened_at_ms INTEGER NOT NULL,
                    closed_at_ms INTEGER,
                    signal_price REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    initial_stop_price REAL NOT NULL,
                    current_stop_price REAL NOT NULL,
                    target_price REAL NOT NULL,
                    quantity REAL NOT NULL,
                    margin_usdt REAL NOT NULL,
                    leverage INTEGER NOT NULL,
                    fee_rate REAL NOT NULL,
                    entry_spread_pct REAL NOT NULL,
                    entry_impact_pct REAL NOT NULL,
                    max_mfe_r REAL NOT NULL DEFAULT 0,
                    max_mae_r REAL NOT NULL DEFAULT 0,
                    runner_armed INTEGER NOT NULL DEFAULT 0,
                    profit_lock_r REAL,
                    exit_price REAL,
                    exit_reason TEXT,
                    gross_pnl REAL,
                    commission REAL,
                    net_pnl REAL,
                    net_pct REAL,
                    result_r REAL,
                    current_price REAL,
                    unrealized_pnl REAL,
                    unrealized_margin_pct REAL,
                    status TEXT NOT NULL DEFAULT 'OPEN',
                    signal_json TEXT NOT NULL,
                    execution_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_real_shadow_open
                    ON real_shadow_trades(status,symbol);
                CREATE TABLE IF NOT EXISTS real_shadow_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp_ms INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                """
            )
            # Existing ledgers predate the live mark columns. Keep the migration
            # additive so a running sample remains intact across upgrades.
            columns = {
                str(row[1]) for row in self.db.execute("PRAGMA table_info(real_shadow_trades)").fetchall()
            }
            for name in ("current_price", "unrealized_pnl", "unrealized_margin_pct"):
                if name not in columns:
                    self.db.execute(f"ALTER TABLE real_shadow_trades ADD COLUMN {name} REAL")

    def close(self) -> None:
        with self.lock:
            self.db.close()

    def _event(
        self, timestamp_ms: int, symbol: str, strategy: str,
        event_type: str, reason: str, payload: dict[str, Any] | None = None,
    ) -> None:
        with self.lock, self.db:
            self.db.execute(
                """INSERT INTO real_shadow_events(
                    timestamp_ms,symbol,strategy,event_type,reason,payload_json
                ) VALUES(?,?,?,?,?,?)""",
                (
                    int(timestamp_ms), str(symbol), str(strategy), str(event_type), str(reason),
                    json.dumps(payload or {}, ensure_ascii=False),
                ),
            )

    @staticmethod
    def _depth_fill(rows: list[Any], quantity: float, buy: bool) -> tuple[float, float, float]:
        remaining, quote, filled = float(quantity), 0.0, 0.0
        best = number(rows[0][0]) if rows else 0.0
        for raw_price, raw_qty in rows or []:
            price, available = number(raw_price), number(raw_qty)
            take = min(remaining, available)
            quote += take * price
            filled += take
            remaining -= take
            if remaining <= 1e-12:
                break
        if best <= 0 or filled <= 0 or remaining > max(1e-12, quantity * 1e-6):
            raise RuntimeError("REAL_SHADOW: profundidade insuficiente")
        vwap = quote / filled
        impact = (vwap - best) / best * 100.0 if buy else (best - vwap) / best * 100.0
        return vwap, best, max(0.0, impact)

    def _quote(self, symbol: str, direction: str, quantity: float) -> dict[str, float]:
        depth_cfg = self.execution_settings.get("depth_filter") or {}
        depth = self.client.get(
            "/fapi/v1/depth",
            {"symbol": symbol, "limit": max(5, min(int(
                depth_cfg.get("levels", self.settings.get("depth_levels", 20))
            ), 100))},
        )
        bids, asks = depth.get("bids") or [], depth.get("asks") or []
        bid, ask = number(bids[0][0]) if bids else 0.0, number(asks[0][0]) if asks else 0.0
        mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else 0.0
        if mid <= 0 or ask < bid:
            raise RuntimeError("REAL_SHADOW: book real invalido")
        buy = str(direction).upper() == "LONG"
        rows = asks if buy else bids
        vwap, best, impact = self._depth_fill(rows, quantity, buy)
        return {
            "price": vwap, "best": best, "impact_pct": impact,
            "spread_pct": (ask - bid) / mid * 100.0,
        }

    def _open_row(self, symbol: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute(
                """SELECT * FROM real_shadow_trades
                   WHERE status='OPEN' AND symbol=? ORDER BY id DESC LIMIT 1""",
                (str(symbol),),
            ).fetchone()
        return dict(row) if row else None

    def _pending(self, row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        signal = json.loads(row["signal_json"])
        execution = json.loads(row["execution_json"])
        execution.update({
            "fill_price": float(row["entry_price"]),
            "initial_stop_price": float(row["initial_stop_price"]),
            "stop_price": float(row["current_stop_price"]),
            "initial_target_price": float(row["target_price"]),
            "target_price": float(row["target_price"]),
            "initial_risk_distance": abs(float(row["entry_price"]) - float(row["initial_stop_price"])),
            "max_mfe_r": float(row["max_mfe_r"]),
            "max_mae_r": float(row["max_mae_r"]),
            "full_runner_armed": bool(row["runner_armed"]),
            "profit_lock_r": row["profit_lock_r"],
        })
        return {
            "id": int(row["id"]), "timestamp_ms": int(row["opened_at_ms"]),
            "symbol": row["symbol"], "direction": row["direction"],
            "signal": signal, "execution": execution,
        }

    def _open(
        self, prepared: dict[str, Any], now_ms: int,
        snapshot: MarketSnapshot | None = None,
    ) -> bool:
        with self.lock:
            open_count = int(self.db.execute(
                "SELECT COUNT(*) FROM real_shadow_trades WHERE status='OPEN'"
            ).fetchone()[0])
        max_open = int(self.execution_settings.get(
            "max_open_positions", self.settings.get("max_open_positions", 10),
        ))
        if max_open > 0 and open_count >= max_open:
            self._event(now_ms, prepared["symbol"], prepared.get("setup", ""), "HOLD", "max_open_positions")
            return False
        concentration = self.campaign.settings.get("directional_concentration") or {}
        concentration_setups = {str(value) for value in concentration.get("setups", [])}
        direction_limit = int(concentration.get("max_same_direction", 0) or 0)
        if (
            bool(concentration.get("enabled", False)) and direction_limit > 0
            and str(prepared.get("setup") or "") in concentration_setups
        ):
            placeholders = ",".join("?" for _ in concentration_setups)
            params = [str(prepared["direction"]), *sorted(concentration_setups)]
            with self.lock:
                same_direction = int(self.db.execute(
                    f"""SELECT COUNT(*) FROM real_shadow_trades
                        WHERE status='OPEN' AND direction=? AND strategy IN ({placeholders})""",
                    params,
                ).fetchone()[0])
            if same_direction >= direction_limit:
                self._event(
                    now_ms, prepared["symbol"], prepared.get("setup", ""), "HOLD",
                    "directional_concentration_limit",
                    {"open_same_direction": same_direction, "limit": direction_limit},
                )
                return False
        reference = float(prepared["entry_price"])
        margin = float(self.execution_settings.get(
            "fixed_margin_usdt", self.settings.get("fixed_margin_usdt", 50.0),
        ))
        leverage = int(self.execution_settings.get(
            "leverage", self.settings.get("leverage", 10),
        ))
        notional = margin * leverage
        estimate_qty = notional / reference if reference > 0 else 0.0
        quote = self._quote(prepared["symbol"], prepared["direction"], estimate_qty)
        fill = float(quote["price"])
        quantity = notional / fill if fill > 0 else 0.0
        # Re-evaluate the exact fixed-notional quantity against production depth.
        quote = self._quote(prepared["symbol"], prepared["direction"], quantity)
        fill = float(quote["price"])
        quantity = notional / fill
        long = str(prepared["direction"]).upper() == "LONG"
        signed_drift = (fill - reference) / reference * 100.0 if reference else 999.0
        adverse = signed_drift if long else -signed_drift
        depth_cfg = self.execution_settings.get("depth_filter") or {}
        checks = {
            "spread": quote["spread_pct"] <= float(self.execution_settings.get(
                "max_entry_execution_spread_pct", self.settings.get("max_entry_spread_pct", 0.25),
            )),
            "impact": quote["impact_pct"] <= float(depth_cfg.get(
                "max_entry_impact_pct", self.settings.get("max_entry_impact_pct", 0.20),
            )),
            "adverse_drift": adverse <= float(self.execution_settings.get(
                "max_entry_drift_pct", self.settings.get("max_entry_adverse_drift_pct", 0.05),
            )),
            "favorable_drift": -adverse <= float(self.execution_settings.get(
                "max_favorable_entry_drift_pct", self.settings.get("max_entry_favorable_drift_pct", 0.20),
            )),
        }
        if not all(checks.values()):
            self._event(now_ms, prepared["symbol"], prepared.get("setup", ""), "REJECT", "real_execution_filter", {
                "checks": checks, "spread_pct": quote["spread_pct"],
                "impact_pct": quote["impact_pct"], "entry_drift_pct": signed_drift,
            })
            return False
        stop, target = float(prepared["stop_price"]), float(prepared["target_price"])
        risk = abs(fill - stop)
        if risk <= 0 or (long and stop >= fill) or (not long and stop <= fill):
            self._event(now_ms, prepared["symbol"], prepared.get("setup", ""), "REJECT", "invalid_fill_geometry")
            return False
        fee_rate = float(self.execution_settings.get(
            "fallback_taker_fee_pct", self.settings.get("fee_pct_per_side", 0.05),
        )) / 100.0
        execution = {
            "mode": "REAL_SHADOW", "fill_price": fill,
            "initial_stop_price": stop, "stop_price": stop,
            "initial_target_price": target, "target_price": target,
            "initial_risk_distance": risk,
            "full_position_runner": bool(prepared.get("full_position_runner")),
            "runner_target_disabled": bool(prepared.get("full_position_runner")),
            "working_type": str(
                (self.execution_settings.get("strategy_working_type_overrides") or {}).get(
                    prepared.get("setup"), self.execution_settings.get("working_type", "MARK_PRICE"),
                )
            ).upper(),
            "max_mfe_r": 0.0, "max_mae_r": 0.0,
        }
        with self.lock, self.db:
            cursor = self.db.execute(
                """INSERT INTO real_shadow_trades(
                    campaign_id,strategy,symbol,direction,opened_at_ms,signal_price,entry_price,
                    initial_stop_price,current_stop_price,target_price,quantity,margin_usdt,leverage,
                    fee_rate,entry_spread_pct,entry_impact_pct,signal_json,execution_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    prepared.get("campaign_id", ""), prepared.get("setup", ""), prepared["symbol"],
                    prepared["direction"], int(now_ms), reference, fill, stop, stop, target, quantity,
                    margin, leverage, fee_rate, quote["spread_pct"], quote["impact_pct"],
                    json.dumps(prepared, ensure_ascii=False), json.dumps(execution, ensure_ascii=False),
                ),
            )
            shadow_trade_id = int(cursor.lastrowid)
        self._event(now_ms, prepared["symbol"], prepared.get("setup", ""), "OPEN", "real_market_shadow", {
            "entry_price": fill, "signal_price": reference, "quantity": quantity,
            "spread_pct": quote["spread_pct"], "impact_pct": quote["impact_pct"],
            "entry_drift_pct": signed_drift,
        })
        if self.leveraged_mirror is not None and snapshot is not None:
            try:
                self.leveraged_mirror.on_real_shadow_open(prepared, {
                    "id": shadow_trade_id,
                    "entry_price": fill,
                    "initial_stop_price": stop,
                    "target_price": target,
                }, snapshot)
            except Exception as exc:
                self._event(now_ms, prepared["symbol"], prepared.get("setup", ""), "ERROR",
                            "leveraged_shadow_mirror_open", {"error": str(exc)})
        return True

    def on_signal(self, signal: dict[str, Any], snapshot: MarketSnapshot) -> bool | None:
        """Return True/False only when a new real-market entry was attempted.

        None means the campaign consumed the signal without requesting a new
        position (for example, confirmation or same-direction hold).
        """
        if not self.enabled or not bool(self.execution_settings.get("new_entries_enabled", True)):
            return None
        now_ms = int(signal.get("timestamp_ms") or snapshot.timestamp_ms)
        row = self._open_row(signal["symbol"])
        pending = self._pending(row)
        decision = self.campaign.decide(signal, pending)
        setup = str(signal.get("setup") or "")
        try:
            if decision.action in {"HOLD", "REJECT", "CONFIRM"}:
                self._event(now_ms, signal["symbol"], setup, decision.action, decision.reason)
                return None
            if decision.action == "FLIP" and row is not None:
                self._close(row, snapshot.price, now_ms, "CAMPAIGN_FLIP")
            return self._open(decision.signal or signal, now_ms, snapshot)
        except Exception as exc:
            self._event(now_ms, signal["symbol"], setup, "ERROR", "real_shadow_signal_error", {"error": str(exc)})
            return False

    def _exit_quote(self, row: dict[str, Any], fallback: float) -> float:
        quantity = float(row["quantity"])
        # To close a LONG we sell into bids; to close a SHORT we buy asks.
        exit_direction = "SHORT" if str(row["direction"]).upper() == "LONG" else "LONG"
        try:
            return float(self._quote(row["symbol"], exit_direction, quantity)["price"])
        except Exception:
            return float(fallback)

    def _close(self, row: dict[str, Any], market_price: float, now_ms: int, reason: str) -> None:
        exit_price = self._exit_quote(row, market_price)
        entry, quantity = float(row["entry_price"]), float(row["quantity"])
        long = str(row["direction"]).upper() == "LONG"
        gross = (exit_price - entry) * quantity if long else (entry - exit_price) * quantity
        commission = (entry + exit_price) * quantity * float(row["fee_rate"])
        net = gross - commission
        notional = entry * quantity
        net_pct = net / notional * 100.0 if notional else 0.0
        risk_quote = quantity * abs(entry - float(row["initial_stop_price"])) + entry * quantity * float(row["fee_rate"]) * 2.0
        result_r = net / risk_quote if risk_quote > 0 else 0.0
        with self.lock, self.db:
            self.db.execute(
                """UPDATE real_shadow_trades SET
                    closed_at_ms=?,exit_price=?,exit_reason=?,gross_pnl=?,commission=?,net_pnl=?,
                    net_pct=?,result_r=?,current_price=?,unrealized_pnl=?,unrealized_margin_pct=?,
                    status='CLOSED' WHERE id=?""",
                (
                    int(now_ms), exit_price, reason, gross, commission, net, net_pct, result_r,
                    exit_price, net, net / float(row["margin_usdt"]) * 100.0 if row["margin_usdt"] else 0.0,
                    int(row["id"]),
                ),
            )
        self._event(now_ms, row["symbol"], row["strategy"], "CLOSE", reason, {
            "entry_price": entry, "exit_price": exit_price, "net_pnl": net, "result_r": result_r,
        })

    def _price_maps(self) -> tuple[dict[str, float], dict[str, float]]:
        books = self.client.get("/fapi/v1/ticker/bookTicker")
        premiums = self.client.get("/fapi/v1/premiumIndex")
        contract = {
            str(row.get("symbol")): (number(row.get("bidPrice")) + number(row.get("askPrice"))) / 2.0
            for row in books if number(row.get("bidPrice")) > 0 and number(row.get("askPrice")) > 0
        }
        mark = {str(row.get("symbol")): number(row.get("markPrice")) for row in premiums}
        return contract, mark

    def refresh(self, latest: dict[str, MarketSnapshot]) -> None:
        if not self.enabled:
            return
        with self.lock:
            rows = [dict(row) for row in self.db.execute(
                "SELECT * FROM real_shadow_trades WHERE status='OPEN' ORDER BY id"
            ).fetchall()]
        mirror_symbols = (
            set(self.leveraged_mirror.leveraged_open_symbols())
            if self.leveraged_mirror is not None else set()
        )
        if not rows and not mirror_symbols:
            return
        now_ms = int(time.time() * 1000)
        try:
            contract_prices, mark_prices = self._price_maps()
        except Exception as exc:
            self._event(now_ms, "SYSTEM", "REAL_SHADOW", "ERROR", "price_refresh_failed", {"error": str(exc)})
            return
        mirrored_prices: set[str] = set()
        for row in rows:
            pending = self._pending(row)
            if pending is None:
                continue
            execution = pending["execution"]
            working_type = str(execution.get("working_type") or "MARK_PRICE").upper()
            prices = contract_prices if working_type == "CONTRACT_PRICE" else mark_prices
            price = float(prices.get(row["symbol"]) or contract_prices.get(row["symbol"]) or 0.0)
            if price <= 0:
                continue
            if self.leveraged_mirror is not None:
                try:
                    self.leveraged_mirror.on_real_shadow_price(row["symbol"], price, now_ms)
                    mirrored_prices.add(str(row["symbol"]))
                except Exception as exc:
                    self._event(now_ms, row["symbol"], row["strategy"], "ERROR",
                                "leveraged_shadow_mirror_price", {"error": str(exc)})
            entry, initial_stop = float(row["entry_price"]), float(row["initial_stop_price"])
            risk = abs(entry - initial_stop)
            long = str(row["direction"]).upper() == "LONG"
            current_r = ((price - entry) if long else (entry - price)) / risk if risk > 0 else 0.0
            max_mfe = max(float(row["max_mfe_r"]), current_r)
            max_mae = min(float(row["max_mae_r"]), current_r)
            quantity = float(row["quantity"])
            gross_live = ((price - entry) if long else (entry - price)) * quantity
            commission_live = (entry + price) * quantity * float(row["fee_rate"])
            net_live = gross_live - commission_live
            margin = float(row["margin_usdt"])
            with self.lock, self.db:
                self.db.execute(
                    """UPDATE real_shadow_trades SET max_mfe_r=?,max_mae_r=?,current_price=?,
                       unrealized_pnl=?,unrealized_margin_pct=? WHERE id=?""",
                    (max_mfe, max_mae, price, net_live, net_live / margin * 100.0 if margin else 0.0, int(row["id"])),
                )
            row["max_mfe_r"], row["max_mae_r"] = max_mfe, max_mae
            stop = float(row["current_stop_price"])
            if (long and price <= stop) or (not long and price >= stop):
                protected = stop >= entry if long else stop <= entry
                self._close(row, price, now_ms, "RUNNER_STOP" if protected else "STOP")
                continue
            signal = pending["signal"]
            full_runner = self.campaign.full_runner_enabled(signal) or bool(execution.get("full_position_runner"))
            target = float(row["target_price"])
            if not full_runner and ((long and price >= target) or (not long and price <= target)):
                self._close(row, price, now_ms, "TARGET")
                continue
            snapshot = latest.get(row["symbol"])
            market = snapshot.to_dict() if snapshot is not None else None
            pending = self._pending(row)
            if pending is None:
                continue
            pending["execution"]["max_mfe_r"] = max_mfe
            decision = self.campaign.manage(pending, price, now_ms, market)
            if decision.action in {"ARM_RUNNER", "PROTECT"} and decision.signal is not None:
                desired = float(decision.signal["stop_price"])
                improves = desired > stop if long else desired < stop
                if improves and ((long and desired < price) or (not long and desired > price)):
                    execution = pending["execution"]
                    execution["stop_price"] = desired
                    execution["profit_lock_r"] = float(decision.signal["lock_r"])
                    if decision.action == "ARM_RUNNER":
                        execution["full_runner_armed"] = True
                    with self.lock, self.db:
                        self.db.execute(
                            """UPDATE real_shadow_trades SET current_stop_price=?,runner_armed=?,
                               profit_lock_r=?,execution_json=? WHERE id=?""",
                            (
                                desired, 1 if execution.get("full_runner_armed") else int(row["runner_armed"]),
                                execution.get("profit_lock_r"), json.dumps(execution, ensure_ascii=False), int(row["id"]),
                            ),
                        )
                    self._event(now_ms, row["symbol"], row["strategy"], decision.action, decision.reason, {
                        "stop_price": desired, "profit_lock_r": execution.get("profit_lock_r"),
                    })
            elif decision.action == "EXIT":
                # Keep the production-market shadow under the same thesis
                # management as Testnet.  Previously this decision was silently
                # ignored, so Shadow held failed launches until the full stop.
                self._close(row, price, now_ms, "THESIS_EXIT")
                self._event(now_ms, row["symbol"], row["strategy"], "THESIS_EXIT", decision.reason, {
                    "current_r": current_r, "max_mfe_r": max_mfe,
                    "failures": (decision.signal or {}).get("failures", []),
                })
        # A tighter half-geometry trade normally closes before its source
        # Shadow position. If it does not, keep feeding it production prices
        # after the source closes instead of leaving an orphaned simulation.
        if self.leveraged_mirror is not None:
            for symbol in mirror_symbols - mirrored_prices:
                price = float(contract_prices.get(symbol) or mark_prices.get(symbol) or 0.0)
                if price > 0:
                    try:
                        self.leveraged_mirror.on_real_shadow_price(symbol, price, now_ms)
                    except Exception as exc:
                        self._event(now_ms, symbol, "LEV20_HALF_GEOMETRY", "ERROR",
                                    "leveraged_shadow_mirror_price", {"error": str(exc)})

    def status(self) -> dict[str, Any]:
        with self.lock:
            rows = [dict(row) for row in self.db.execute(
                """SELECT * FROM real_shadow_trades
                   ORDER BY CASE WHEN status='OPEN' THEN 0 ELSE 1 END, id DESC"""
            ).fetchall()]
        closed = [row for row in rows if row["status"] == "CLOSED"]
        opened = [row for row in rows if row["status"] == "OPEN"]
        wins = [float(row["net_pnl"]) for row in closed if float(row["net_pnl"] or 0.0) > 0]
        losses = [float(row["net_pnl"]) for row in closed if float(row["net_pnl"] or 0.0) < 0]
        closed_pnl = sum(float(row["net_pnl"] or 0.0) for row in closed)
        open_unrealized_pnl = sum(float(row["unrealized_pnl"] or 0.0) for row in opened)
        by_strategy: dict[str, dict[str, Any]] = {}
        for name in sorted({str(row["strategy"]) for row in rows}):
            selected = [row for row in rows if str(row["strategy"]) == name]
            done = [row for row in selected if row["status"] == "CLOSED"]
            pnl = [float(row["net_pnl"] or 0.0) for row in done]
            gp, gl = sum(value for value in pnl if value > 0), -sum(value for value in pnl if value < 0)
            by_strategy[name] = {
                "open": sum(row["status"] == "OPEN" for row in selected), "closed": len(done),
                "net_pnl": sum(pnl), "win_rate_pct": sum(value > 0 for value in pnl) / len(pnl) * 100.0 if pnl else 0.0,
                "profit_factor": gp / gl if gl else None,
            }
        return {
            "enabled": self.enabled,
            "summary": {
                "open": len(opened), "closed": len(closed),
                "net_pnl": closed_pnl,
                "closed_pnl": closed_pnl,
                "open_unrealized_pnl": open_unrealized_pnl,
                "total_pnl": closed_pnl + open_unrealized_pnl,
                "win_rate_pct": len(wins) / len(closed) * 100.0 if closed else 0.0,
                "profit_factor": sum(wins) / -sum(losses) if losses else None,
            },
            "by_strategy": by_strategy,
            "recent_trades": rows[:200],
        }
