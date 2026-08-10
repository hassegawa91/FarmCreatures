from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from engine.binance import BinancePublicClient, MarketSnapshot
from engine.border_regime import BorderRegimeStrategy
from engine.campaign import CampaignPolicy
from engine.journal import TradeJournal
from engine.execution import ExecutionRouter
from engine.dump_reclaim import DumpExhaustionReclaimStrategy
from engine.strategy import StrategyEvent
from engine.simulation import ParallelStrategyLab
from engine.real_shadow import RealMarketShadow
from engine.volatility_scalp import VolatilityExhaustionFadeScalpStrategy


class TradingService:
    def __init__(self, config: dict[str, Any], root: Path):
        self.config = config
        self.strategy_config = config["strategy"]
        self.campaign_config = config.get("campaign") or {}
        self.campaign = CampaignPolicy(self.campaign_config)
        self.strategy_modes = {
            str(name): str(mode).upper()
            for name, mode in (config.get("strategy_modes") or {}).items()
        }
        timeout = float(config.get("http_timeout_seconds", 12))
        self.client = BinancePublicClient(timeout)
        chart_base = (
            config["execution"]["testnet_base_url"]
            if config["mode"] == "TESTNET" else config["execution"]["real_base_url"]
        )
        self.chart_client = BinancePublicClient(timeout, chart_base)
        self.dump_reclaim = DumpExhaustionReclaimStrategy(self.strategy_config)
        self.border_regime = BorderRegimeStrategy(self.strategy_config)
        self.volatility_scalp = VolatilityExhaustionFadeScalpStrategy(self.strategy_config)
        self.strategy = self.dump_reclaim
        self.strategies = [self.dump_reclaim, self.border_regime, self.volatility_scalp]
        self.journal = TradeJournal(
            root / config["database_path"], self.strategy_config["fee_pct_per_side"],
            self.campaign_config,
        )
        self.execution = ExecutionRouter(config, root)
        lab_config = config.get("simulation_lab") or {}
        self.simulation_lab = ParallelStrategyLab(
            lab_config, root / str(lab_config.get("database_path", "data/simulations.sqlite")),
        )
        real_shadow_config = config.get("real_shadow") or {}
        self.real_shadow = RealMarketShadow(
            real_shadow_config, config["execution"], self.campaign, self.client,
            root / str(real_shadow_config.get("database_path", "data/real_shadow.sqlite")),
        )
        limited_shadow_config = config.get("limited_shadow") or {}
        limited_execution = {
            **config["execution"],
            "max_open_positions": int(limited_shadow_config.get("max_open_positions", 2)),
        }
        limited_settings = {
            **real_shadow_config,
            **limited_shadow_config,
            "max_open_positions": int(limited_shadow_config.get("max_open_positions", 2)),
        }
        self.limited_shadow = RealMarketShadow(
            limited_settings, limited_execution, self.campaign, self.client,
            root / str(limited_shadow_config.get("database_path", "data/limited_shadow.sqlite")),
        )
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.live_thread: threading.Thread | None = None
        self.execution_lock = threading.RLock()
        self.universe: list[str] = []
        self.entry_eligible_symbols: set[str] | None = None
        self.universe_updated_at = 0.0
        self.prefiltered_count = 0
        self.lab_candidate_count = 0
        self.latest: dict[str, MarketSnapshot] = {}
        self.last_scan_ms = 0
        self.scan_duration_seconds = 0.0
        self.errors: list[str] = []
        self.execution_state: dict[str, Any] = {"mode": config["mode"], "positions": [], "orders": [], "algo_orders": []}
        self.account_snapshot: dict[str, Any] | None = None
        self.live_last_update_ms = 0

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._loop, name="v10-engine", daemon=True)
        self.thread.start()
        if self.execution.broker is not None or self.real_shadow.enabled or self.limited_shadow.enabled:
            self.live_thread = threading.Thread(target=self._live_loop, name="v10-binance-live", daemon=True)
            self.live_thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=10)
        if self.live_thread:
            self.live_thread.join(timeout=10)
        self.journal.close()
        self.simulation_lab.close()
        self.real_shadow.close()
        self.limited_shadow.close()

    def _refresh_execution_state(self) -> None:
        if self.execution.broker is None:
            return
        try:
            with self.execution_lock:
                monitored = set(self.journal.open_symbols())
                state = self.execution.account_state(monitored)
                account = self.execution.broker.account()
                pending_before = {
                    str(item["symbol"]): item for item in self.journal.pending_executions()
                }
                completed = self.execution.reconcile(self.journal, state)
                for closed in completed:
                    prior = pending_before.get(str(closed.get("symbol") or ""))
                    if prior is None:
                        continue
                    latest = self.latest.get(str(closed.get("symbol") or ""))
                    reentry = self.dump_reclaim.arm_reentry(
                        prior["signal"], closed, latest,
                    )
                    if reentry is not None:
                        self.journal.record_event(reentry)
                if completed:
                    state = self.execution.account_state(set(self.journal.open_symbols()))
                if self.campaign_config.get("enabled"):
                    state = self._manage_live_campaigns(state)
            account_snapshot = {
                "canTrade": account.get("canTrade"),
                "totalWalletBalance": account.get("totalWalletBalance"),
                "totalMarginBalance": account.get("totalMarginBalance"),
                "availableBalance": account.get("availableBalance"),
                "totalUnrealizedProfit": account.get("totalUnrealizedProfit"),
            }
            with self.lock:
                self.execution_state = state
                self.account_snapshot = account_snapshot
                self.live_last_update_ms = int(time.time() * 1000)
        except Exception as exc:
            with self.lock:
                self.errors.append(f"binance_live: {exc}")
                self.errors = self.errors[-20:]

    def _campaign_event(
        self, action: str, symbol: str, direction: str, reason: str, payload: dict[str, Any] | None = None,
    ) -> None:
        self.journal.record_event(StrategyEvent(
            f"CAMPAIGN_{action}", symbol, direction, int(time.time() * 1000), reason, payload or {},
        ))

    def _manage_live_campaigns(self, state: dict[str, Any]) -> dict[str, Any]:
        positions = {str(row.get("symbol")): row for row in state.get("positions") or []}
        now_ms = int(time.time() * 1000)
        for pending in self.journal.pending_executions():
            symbol = str(pending["symbol"])
            position = positions.get(symbol)
            if position is None:
                continue
            mark = abs(float(position.get("markPrice") or 0.0))
            setup = str((pending.get("signal") or {}).get("setup") or "")
            result = pending["execution"]
            working_type = str(
                result.get("working_type")
                or (self.config["execution"].get("strategy_working_type_overrides") or {}).get(setup)
                or self.config["execution"].get("working_type", "MARK_PRICE")
            ).upper()
            management_price = mark
            if working_type == "CONTRACT_PRICE":
                try:
                    management_price = self.execution.contract_price(symbol)
                    position["contractPrice"] = management_price
                    position["managementPriceType"] = "CONTRACT_PRICE"
                except Exception as exc:
                    self.errors.append(f"runner_contract_price {symbol}: {exc}")
                    self.errors = self.errors[-20:]
                    continue
            latest = self.latest.get(symbol)
            market = latest.to_dict() if latest is not None else None
            decision = self.campaign.manage(pending, management_price, now_ms, market)
            if decision.action == "ENABLE_RUNNER":
                enabled = self.execution.disable_take_profit(symbol)
                result.update(enabled)
                result["full_position_runner"] = True
                self.journal.update_execution_payload(pending["id"], result)
                self._campaign_event("RUNNER_ENABLED", symbol, pending["direction"], decision.reason, {
                    "campaign_id": pending["signal"].get("campaign_id"), **enabled,
                })
                state = self.execution.account_state({row["symbol"] for row in self.journal.pending_executions()})
                positions = {str(row.get("symbol")): row for row in state.get("positions") or []}
            elif decision.action == "ARM_RUNNER" and decision.signal is not None:
                requested_stop = self.execution.normalized_stop_price(
                    symbol, pending["direction"], float(decision.signal["stop_price"]),
                )
                current_stop = float(result.get("stop_price") or result.get("initial_stop_price") or 0.0)
                improves_tick = (
                    requested_stop > current_stop
                    if pending["direction"] == "LONG" else requested_stop < current_stop
                )
                if not improves_tick:
                    self.journal.update_execution_payload(pending["id"], result)
                    continue
                protection = self.execution.update_stop(
                    symbol, pending["direction"], requested_stop, working_type,
                )
                result.update(protection)
                result["full_position_runner"] = True
                result["full_runner_armed"] = True
                result["runner_armed_at_ms"] = now_ms
                result["profit_lock_r"] = float(decision.signal["lock_r"])
                self.journal.update_execution_payload(pending["id"], result)
                self._campaign_event("RUNNER_ARMED", symbol, pending["direction"], decision.reason, {
                    "campaign_id": pending["signal"].get("campaign_id"),
                    "profit_lock_r": result["profit_lock_r"], **protection,
                })
            elif decision.action == "PARTIAL":
                runner_stop = self.campaign.runner_stop(pending, mark, market)
                partial = self.execution.take_partial(
                    symbol, pending["direction"], float(self.campaign_config.get("partial_fraction", 0.50)),
                    runner_stop, float(result["target_price"]),
                    float(result.get("initial_stop_price") or result.get("stop_price") or 0.0),
                )
                if partial.get("deferred"):
                    self._campaign_event("PARTIAL_DEFERRED", symbol, pending["direction"], partial["reason"], {
                        "campaign_id": pending["signal"].get("campaign_id"), **partial,
                    })
                    result["partial_deferred"] = partial
                    self.journal.update_execution_payload(pending["id"], result)
                    continue
                result.update(partial)
                result["partial_taken"] = True
                result["runner_structure_stop_price"] = partial.get("stop_price")
                self.journal.update_execution_payload(pending["id"], result)
                self._campaign_event("PARTIAL", symbol, pending["direction"], decision.reason, {
                    "campaign_id": pending["signal"].get("campaign_id"), **partial,
                })
                state = self.execution.account_state({row["symbol"] for row in self.journal.pending_executions()})
                positions = {str(row.get("symbol")): row for row in state.get("positions") or []}
            elif decision.action == "PROTECT" and decision.signal is not None:
                requested_stop = self.execution.normalized_stop_price(
                    symbol, pending["direction"], float(decision.signal["stop_price"]),
                )
                current_stop = float(result.get("stop_price") or result.get("initial_stop_price") or 0.0)
                improves_tick = (
                    requested_stop > current_stop
                    if pending["direction"] == "LONG" else requested_stop < current_stop
                )
                if not improves_tick:
                    self.journal.update_execution_payload(pending["id"], result)
                    continue
                protection = self.execution.update_stop(
                    symbol, pending["direction"], requested_stop, working_type,
                )
                result.update(protection)
                result["profit_lock_r"] = float(decision.signal["lock_r"])
                self.journal.update_execution_payload(pending["id"], result)
                self._campaign_event("PROTECT", symbol, pending["direction"], decision.reason, {
                    "campaign_id": pending["signal"].get("campaign_id"),
                    "profit_lock_r": result["profit_lock_r"], **protection,
                })
            elif decision.action == "FLIP" and decision.signal is not None:
                closed = self.execution.close_symbol(symbol, "CAMPAIGN_FLIP")
                self._campaign_event("FLIP_CLOSE", symbol, pending["direction"], decision.reason, {
                    "campaign_id": pending["signal"].get("campaign_id"),
                    "failures": decision.signal.get("evidence", {}).get("reversal_failures", []), **closed,
                })
                state = self.execution.account_state({symbol})
                self.execution.reconcile(self.journal, state)
                if bool(self.campaign_config.get("signal_flip_immediate_reentry", False)):
                    self._execute_campaign_entry(decision.signal, "FLIP", decision.reason)
                else:
                    self._campaign_event("FLIP_REENTRY_DEFERRED", symbol, decision.signal["direction"],
                                         "awaiting_fresh_executable_confirmation", {
                                             "setup": decision.signal.get("setup"),
                                         })
                state = self.execution.account_state({row["symbol"] for row in self.journal.pending_executions()})
                positions = {str(row.get("symbol")): row for row in state.get("positions") or []}
            elif decision.action == "EXIT":
                closed = self.execution.close_symbol(symbol, "THESIS_EXIT")
                self.journal.close_reference(symbol, mark, now_ms, "REFERENCE_THESIS_EXIT")
                self._campaign_event("EXIT", symbol, pending["direction"], decision.reason, {
                    "campaign_id": pending["signal"].get("campaign_id"),
                    "max_mfe_r": result.get("max_mfe_r"),
                    "current_r": result.get("current_r"),
                    "failures": (decision.signal or {}).get("failures", []), **closed,
                })
                state = self.execution.account_state({row["symbol"] for row in self.journal.pending_executions()})
                self.execution.reconcile(self.journal, state)
                state = self.execution.account_state({row["symbol"] for row in self.journal.pending_executions()})
                positions = {str(row.get("symbol")): row for row in state.get("positions") or []}
            else:
                self.journal.update_execution_payload(pending["id"], result)
        return state

    def _execute_campaign_entry(self, signal: dict[str, Any], action: str, reason: str) -> None:
        if action in {"ENTER", "FLIP"}:
            replaced = self.journal.close_reference(
                signal["symbol"], float(signal["entry_price"]), int(signal["timestamp_ms"]),
            )
            if replaced:
                self.journal.record_event(StrategyEvent(
                    "REFERENCE_CLOSED", signal["symbol"], signal["direction"],
                    int(signal["timestamp_ms"]), "reference_replaced_by_new_signal", replaced,
                ))
        try:
            result = self.execution.execute(
                signal, self.journal, int(self.strategy_config["max_concurrent_shadow_positions"]),
            )
            if result.get("safety_closed"):
                result_type = "EXECUTION_SAFETY_CLOSED"
            else:
                result_type = "EXECUTION_OPENED" if result.get("ok") else "SIGNAL_BLOCKED"
            result_reason = result.get("reason") or reason
        except Exception as exc:
            result = {"ok": False, "mode": self.config["mode"], "error": str(exc)}
            result_type, result_reason = "EXECUTION_FAILED", "broker_error"
        self.journal.record_execution(signal, result)
        self.journal.record_event(StrategyEvent(
            result_type, signal["symbol"], signal["direction"], signal["timestamp_ms"], result_reason,
            {**result, "_strategy": signal.get("setup"), "campaign_action": action},
        ))
        if result.get("safety_closed"):
            state = self.execution.account_state({signal["symbol"]})
            self.execution.reconcile(self.journal, state)
        if (
            result.get("ok") and result.get("position_open", True)
            and str(result.get("mode") or "").upper() != "SHADOW"
        ):
            reference_ok, reference_reason = self.journal.record_signal(
                signal, int(self.strategy_config["max_concurrent_shadow_positions"]), "REFERENCE",
            )
            self.journal.record_event(StrategyEvent(
                "REFERENCE_OPENED" if reference_ok else "REFERENCE_BLOCKED",
                signal["symbol"], signal["direction"], int(signal["timestamp_ms"]), reference_reason,
                {"setup": signal.get("setup"), "campaign_action": action},
            ))
        self._campaign_event(action, signal["symbol"], signal["direction"], reason, {
            "campaign_id": signal.get("campaign_id"), "setup": signal.get("setup"), **result,
        })

    def _handle_campaign_signal(self, signal: dict[str, Any]) -> None:
        circuit = self.journal.symbol_circuit_status(
            str(signal.get("symbol") or ""), int(signal.get("timestamp_ms") or time.time() * 1000),
            self.config["execution"].get("symbol_circuit_breaker") or {},
        )
        if circuit.get("blocked"):
            self._campaign_event(
                "HOLD", signal["symbol"], signal["direction"],
                "symbol_execution_circuit_breaker", {"setup": signal.get("setup"), **circuit},
            )
            return
        if (
            self.entry_eligible_symbols is not None
            and str(signal.get("symbol") or "") not in self.entry_eligible_symbols
        ):
            self._campaign_event(
                "REJECT", signal["symbol"], signal["direction"],
                "standard_margin_not_supported",
                {"setup": signal.get("setup"), "fixed_margin_usdt": self.config["execution"].get("fixed_margin_usdt")},
            )
            return
        pending_executions = self.journal.pending_executions()
        pending = next(
            (item for item in pending_executions if item["symbol"] == signal["symbol"]), None,
        )
        setup = str(signal.get("setup") or "")
        concentration = self.campaign_config.get("directional_concentration") or {}
        concentration_setups = {str(value) for value in concentration.get("setups", [])}
        direction_limit = int(concentration.get("max_same_direction", 0) or 0)
        if (
            pending is None and bool(concentration.get("enabled", False))
            and direction_limit > 0 and setup in concentration_setups
        ):
            same_direction = sum(
                str((item.get("signal") or {}).get("setup") or "") in concentration_setups
                and str(item.get("direction") or "") == str(signal.get("direction") or "")
                for item in pending_executions
            )
            if same_direction >= direction_limit:
                self._campaign_event(
                    "HOLD", signal["symbol"], signal["direction"],
                    "directional_concentration_limit",
                    {"setup": setup, "open_same_direction": same_direction, "limit": direction_limit},
                )
                return
        strategy_limits = self.campaign_config.get("strategy_max_open_positions") or {}
        strategy_limit = int(strategy_limits.get(setup, 0) or 0)
        if pending is None and strategy_limit > 0:
            strategy_open = sum(
                str((item.get("signal") or {}).get("setup") or "") == setup
                for item in pending_executions
            )
            if strategy_open >= strategy_limit:
                self._campaign_event(
                    "HOLD", signal["symbol"], signal["direction"],
                    "strategy_open_position_limit",
                    {"setup": setup, "open": strategy_open, "limit": strategy_limit},
                )
                return
        decision = self.campaign.decide(signal, pending)
        if decision.action in {"HOLD", "REJECT"}:
            self._campaign_event(decision.action, signal["symbol"], signal["direction"], decision.reason, {
                "setup": signal.get("setup"),
            })
            return
        if decision.action == "CONFIRM" and pending is not None:
            result = pending["execution"]
            result["campaign_confirmed"] = True
            result["confirmed_at_ms"] = int(signal["timestamp_ms"])
            result["confirmation_setup"] = signal.get("setup")
            self.journal.update_execution_payload(pending["id"], result)
            self._campaign_event("CONFIRM", signal["symbol"], signal["direction"], decision.reason, {
                "campaign_id": pending["signal"].get("campaign_id"), "setup": signal.get("setup"),
            })
            return
        if decision.action == "FLIP" and pending is not None:
            closed = self.execution.close_symbol(signal["symbol"], "CAMPAIGN_FLIP")
            self._campaign_event("FLIP_CLOSE", signal["symbol"], signal["direction"], decision.reason, {
                "campaign_id": pending["signal"].get("campaign_id"), "closed": closed,
            })
            state = self.execution.account_state({signal["symbol"]})
            self.execution.reconcile(self.journal, state)
            if not bool(self.campaign_config.get("signal_flip_immediate_reentry", False)):
                self._campaign_event(
                    "FLIP_REENTRY_DEFERRED", signal["symbol"], signal["direction"],
                    "awaiting_fresh_executable_confirmation", {"setup": signal.get("setup")},
                )
                return
        self._execute_campaign_entry(decision.signal or signal, decision.action, decision.reason)

    def _live_loop(self) -> None:
        interval = max(1.0, float(self.config.get("live_account_refresh_seconds", 2)))
        while not self.stop_event.is_set():
            started = time.monotonic()
            self._refresh_execution_state()
            try:
                with self.lock:
                    latest = dict(self.latest)
                self.real_shadow.refresh(latest)
                self.limited_shadow.refresh(latest)
            except Exception as exc:
                with self.lock:
                    self.errors.append(f"real_shadow_live: {exc}")
                    self.errors = self.errors[-20:]
            remaining = max(0.0, interval - (time.monotonic() - started))
            self.stop_event.wait(remaining)

    def _refresh_universe(self) -> None:
        now = time.time()
        if self.universe and now - self.universe_updated_at < float(self.config["universe_refresh_seconds"]):
            return
        universe_size = int(self.config["universe_size"])
        candidate_size = universe_size * 3 if universe_size > 0 and self.execution.broker is not None else universe_size
        symbols = self.client.universe(
            candidate_size,
            0.0,
            list(self.config.get("always_include") or []),
        )
        supported = self.execution.supported_symbols()
        if supported is not None:
            symbols = [symbol for symbol in symbols if symbol in supported]
        entry_eligible = self.execution.standard_margin_symbols()
        if universe_size > 0:
            symbols = symbols[:universe_size]
        with self.lock:
            self.universe = symbols
            self.entry_eligible_symbols = entry_eligible
            self.universe_updated_at = now

    def _scan(self) -> None:
        started = time.time()
        self.errors = []
        self._refresh_universe()
        monitored = set(self.journal.open_symbols())
        monitored.update(self.simulation_lab.open_symbols())
        for strategy in self.strategies:
            monitored.update(strategy.states)
            monitored.update(getattr(strategy, "deferred_fades", {}))
        monitored.update(str(row.get("symbol")) for row in self.execution_state.get("positions", []))
        symbols = list(dict.fromkeys(self.universe + sorted(symbol for symbol in monitored if symbol)))
        contexts: dict[str, dict[str, Any]] = {}
        workers = min(int(self.config.get("http_workers", 12)), max(1, len(symbols)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="v10-prefilter") as pool:
            futures = {pool.submit(self.client.price_context, symbol, self.strategy_config): symbol for symbol in symbols}
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    contexts[symbol] = future.result()
                except Exception as exc:
                    self.errors.append(f"prefilter {symbol}: {exc}")
        self.journal.update_feature_outcomes(contexts, int(time.time() * 1000))
        self.volatility_scalp.set_market_context(contexts.get("BTCUSDT"))
        min_quote_volume = float(self.config["min_quote_volume_24h"])
        liquid_symbols = {
            symbol for symbol in symbols
            if self.client.quote_volumes.get(symbol, 0.0) >= min_quote_volume
        }
        scalp_candidates = {
            symbol for symbol, context in contexts.items()
            if symbol in liquid_symbols and self.volatility_scalp.prefilter_context(context)
        }
        candidates = [
            symbol for symbol, context in contexts.items()
            if (
                (bool(context["passes"]) and symbol in liquid_symbols)
                or symbol in scalp_candidates
                or symbol in monitored
            )
        ]
        lab_limit = max(0, int((self.config.get("simulation_lab") or {}).get("max_candidates_per_scan", 60)))
        lab_ranked: list[tuple[float, str]] = []
        if self.simulation_lab.enabled and lab_limit:
            for symbol, context in contexts.items():
                if symbol not in liquid_symbols:
                    continue
                adx_value = float(context.get("adx14") or 0.0)
                impulse = abs(float(context.get("price_change_15m_pct") or 0.0))
                width = float(context.get("range_width_pct") or 999.0)
                compression = float(context.get("compression_ratio") or 999.0)
                trend_candidate = adx_value >= 18.0 and impulse >= 0.50
                grid_candidate = 0 < adx_value < 25.0 and 0.40 <= width <= 3.0 and compression <= 1.20
                if trend_candidate or grid_candidate:
                    priority = (impulse + adx_value / 100.0) if trend_candidate else (3.0 - width + (25.0 - adx_value) / 100.0)
                    lab_ranked.append((priority, symbol))
            lab_ranked.sort(reverse=True)
            lab_symbols = [symbol for _, symbol in lab_ranked[:lab_limit]]
            candidates = list(dict.fromkeys(candidates + lab_symbols))
            self.lab_candidate_count = len(lab_symbols)
        else:
            self.lab_candidate_count = 0
        self.prefiltered_count = len({
            symbol for symbol, context in contexts.items()
            if symbol in liquid_symbols and (bool(context["passes"]) or symbol in scalp_candidates)
        })
        snapshots: dict[str, MarketSnapshot] = {}
        workers = min(int(self.config.get("http_workers", 12)), max(1, len(candidates)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="v10-data") as pool:
            futures = {
                pool.submit(self.client.snapshot, symbol, self.strategy_config, contexts.get(symbol)): symbol
                for symbol in candidates
            }
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    snapshots[symbol] = future.result()
                except Exception as exc:
                    self.errors.append(f"{symbol}: {exc}")
        for symbol, snapshot in snapshots.items():
            self.simulation_lab.process(snapshot)
            diagnostics = [strategy.diagnose(snapshot) for strategy in self.strategies]
            self.journal.record_feature_observation(snapshot, diagnostics)
            closed = self.journal.update_position(snapshot)
            if closed:
                event = StrategyEvent(
                    "REFERENCE_CLOSED", symbol, closed["direction"], snapshot.timestamp_ms,
                    closed["reason"], {**closed, "_strategy": closed.get("setup")},
                )
                self.journal.record_event(event)
            for strategy in self.strategies:
                event = strategy.observe(snapshot)
                if not event:
                    continue
                strategy_name = str(getattr(strategy, "name", self.strategy_config["name"]))
                tagged_payload = {**event.payload, "_strategy": strategy_name}
                tagged_event = StrategyEvent(
                    event.type, event.symbol, event.direction, event.timestamp_ms, event.reason, tagged_payload,
                )
                self.journal.record_event(tagged_event)
                if event.type == "SIGNAL":
                    strategy_mode = self.strategy_modes.get(strategy_name, "OBSERVE")
                    if strategy_mode != "EXECUTE":
                        self.journal.record_event(StrategyEvent(
                            "OBSERVATION_SIGNAL", symbol, event.direction, event.timestamp_ms,
                            "strategy_observation_only",
                            {"ok": True, "mode": "OBSERVE", "_strategy": strategy_name},
                        ))
                        continue
                    simulation_signal = (
                        self.campaign.prepare(event.payload) if self.campaign_config.get("enabled")
                        else event.payload
                    )
                    if simulation_signal is not None:
                        self.simulation_lab.on_strategy_signal(simulation_signal, snapshot)
                    real_admission = self.real_shadow.on_signal(event.payload, snapshot)
                    self.limited_shadow.on_signal(event.payload, snapshot)
                    observation_only_setups = {
                        str(item) for item in self.config["execution"].get(
                            "testnet_observation_only_setups", []
                        )
                    }
                    if (
                        self.config["mode"] == "TESTNET"
                        and str(event.payload.get("setup") or "") in observation_only_setups
                    ):
                        self.journal.record_event(StrategyEvent(
                            "SIGNAL_BLOCKED", symbol, event.direction, event.timestamp_ms,
                            "testnet_observation_only_setup",
                            {"ok": False, "mode": "TESTNET", "_strategy": strategy_name},
                        ))
                        continue
                    if (
                        self.config["mode"] == "TESTNET"
                        and bool(self.config["execution"].get("testnet_require_real_market_admission", False))
                        and real_admission is False
                    ):
                        self.journal.record_event(StrategyEvent(
                            "SIGNAL_BLOCKED", symbol, event.direction, event.timestamp_ms,
                            "real_market_admission_rejected",
                            {"ok": False, "mode": "TESTNET", "_strategy": strategy_name},
                        ))
                        continue
                    if self.campaign_config.get("enabled"):
                        with self.execution_lock:
                            self._handle_campaign_signal(event.payload)
                        continue
                    try:
                        with self.execution_lock:
                            result = self.execution.execute(
                                event.payload, self.journal,
                                int(self.strategy_config["max_concurrent_shadow_positions"]),
                            )
                        if result.get("safety_closed"):
                            result_type = "EXECUTION_SAFETY_CLOSED"
                        else:
                            result_type = "EXECUTION_OPENED" if result.get("ok") else "SIGNAL_BLOCKED"
                        result_reason = result.get("reason") or result_type.lower()
                    except Exception as exc:
                        result = {"ok": False, "mode": self.config["mode"], "error": str(exc)}
                        result_type, result_reason = "EXECUTION_FAILED", "broker_error"
                    self.journal.record_execution(event.payload, result)
                    if result.get("safety_closed"):
                        state = self.execution.account_state({symbol})
                        self.execution.reconcile(self.journal, state)
                    self.journal.record_event(
                        StrategyEvent(
                            result_type, symbol, event.direction, event.timestamp_ms, result_reason,
                            {**result, "_strategy": strategy_name},
                        )
                    )
        with self.lock:
            self.latest = snapshots
            self.last_scan_ms = int(time.time() * 1000)
            self.scan_duration_seconds = time.time() - started
            self.errors = self.errors[-20:]

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            cycle_started = time.monotonic()
            try:
                self._scan()
            except Exception as exc:
                self.errors.append(str(exc))
            target_seconds = float(self.config.get("scan_seconds", 60))
            remaining = max(0.0, target_seconds - (time.monotonic() - cycle_started))
            self.stop_event.wait(remaining)

    def status(self) -> dict[str, Any]:
        with self.lock:
            summary = (
                self.journal.summary() if self.config["mode"] == "SHADOW"
                else self.journal.execution_summary()
            )
            strategy_performance = self.journal.execution_summary_by_strategy()
            if self.config["mode"] in {"TESTNET", "REAL"}:
                actual_symbols = {
                    str(row.get("symbol") or "")
                    for row in self.execution_state.get("positions") or []
                }
                pending = self.journal.pending_executions()
                summary["ledger_pending"] = int(summary.get("open") or 0)
                summary["open"] = len(actual_symbols)
                for stats in strategy_performance.values():
                    stats["ledger_pending"] = int(stats.get("open") or 0)
                    stats["open"] = 0
                for item in pending:
                    if str(item.get("symbol") or "") not in actual_symbols:
                        continue
                    setup = str((item.get("signal") or {}).get("setup") or "UNKNOWN")
                    strategy_performance.setdefault(setup, {"open": 0})["open"] += 1
            return {
                "app": self.config["app_name"],
                "mode": self.config["mode"],
                "execution_enabled": bool(self.config["execution"].get("enabled")),
                "new_entries_enabled": bool(self.config["execution"].get("new_entries_enabled", True)),
                "strategy": self.strategy_config["name"],
                "strategies": [strategy.name for strategy in self.strategies],
                "strategy_modes": dict(self.strategy_modes),
                "campaign_enabled": bool(self.campaign_config.get("enabled")),
                "campaign_actions": self.journal.recent_campaign_actions(50),
                "running": bool(self.thread and self.thread.is_alive()),
                "last_scan_ms": self.last_scan_ms,
                "scan_duration_seconds": round(self.scan_duration_seconds, 3),
                "live_last_update_ms": self.live_last_update_ms,
                "live_age_ms": max(0, int(time.time() * 1000) - self.live_last_update_ms) if self.live_last_update_ms else None,
                "universe": self.universe,
                "universe_count": len(self.universe),
                "entry_eligible_count": (
                    len(set(self.universe) & self.entry_eligible_symbols)
                    if self.entry_eligible_symbols is not None else len(self.universe)
                ),
                "liquid_universe_count": sum(
                    self.client.quote_volumes.get(symbol, 0.0) >= float(self.config["min_quote_volume_24h"])
                    for symbol in self.universe
                ),
                "prefiltered_count": self.prefiltered_count,
                "simulation_candidate_count": self.lab_candidate_count,
                "tracked_setups": {
                    strategy.name: strategy.snapshot() for strategy in self.strategies
                },
                "latest": {symbol: snapshot.to_dict() for symbol, snapshot in self.latest.items()},
                "ranking": sorted(
                    (
                        {**strategy.diagnose(snapshot), "market": snapshot.to_dict()}
                        for strategy in self.strategies for snapshot in self.latest.values()
                    ),
                    key=lambda row: (
                        row.get("phase") in {
                            "ARMED", "PULLBACK", "PRE_ARMED", "SQUEEZE_ARMED",
                            "ACCUMULATION_ARMED", "BREAKOUT_DETECTED", "EXTREME_ARMED", "CHOCH_DETECTED",
                            "MOMENTUM_ARMED", "MOMENTUM_PULLBACK", "MICRO_TURN",
                            "MICRO_ARMED", "MICRO_PULLBACK",
                            "TREND_ARMED", "TREND_PULLBACK",
                            "DUMP_ARMED",
                            "BORDER_ARMED", "BORDER_BREAKOUT",
                        }, row.get("passed", 0),
                        abs(row["market"]["price_change_15m_pct"]),
                    ),
                    reverse=True,
                ),
                "summary": summary,
                "strategy_performance": strategy_performance,
                "observation_summary": self.journal.summary(),
                "reference_summary": self.journal.summary(),
                "research_sample": self.journal.feature_observation_summary(),
                "simulation_lab": self.simulation_lab.status(),
                "real_shadow": self.real_shadow.status(),
                "limited_shadow": self.limited_shadow.status(),
                "reference_ledger": self.journal.recent_signals(200),
                "execution_state": self.execution_state,
                "account": self.account_snapshot,
                "errors": list(self.errors),
            }

    def close_all(self, confirmation: str) -> dict[str, Any]:
        expected = f"CLOSE_ALL_{str(self.config['mode']).upper()}"
        if confirmation != expected:
            raise ValueError(f"confirmacao invalida; esperado {expected}")
        with self.execution_lock:
            result = self.execution.close_all()
        with self.lock:
            self.execution_state = result.get("remaining") or self.execution_state
        return result
