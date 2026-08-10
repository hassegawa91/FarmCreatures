from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CampaignDecision:
    action: str
    reason: str
    signal: dict[str, Any] | None = None


class CampaignPolicy:
    """Transforms independent strategy signals into one position campaign per symbol."""

    DEFAULT_ROLES = {
        "DUMP_EXHAUSTION_RECLAIM_V1": "CONFIRM",
        "DUMP_REVERSAL_LONG": "CONFIRM",
        "DUMP_CONTINUATION_SHORT": "CONFIRM",
        "RANGE_EDGE_FADE_LONG": "CONFIRM",
        "RANGE_EDGE_FADE_SHORT": "CONFIRM",
        "BORDER_BREAKOUT_RETEST_LONG": "CONFIRM",
        "BORDER_BREAKOUT_RETEST_SHORT": "CONFIRM",
        "FAILED_BREAKOUT_REVERSAL_LONG": "CONFIRM",
        "FAILED_BREAKOUT_REVERSAL_SHORT": "CONFIRM",
        "FLOW_LONG_CAMPAIGN": "CONFIRM",
        "FLOW_STRUCTURAL_REVERSAL": "REVERSAL",
        "FLOW_TREND_PULLBACK": "CONFIRM",
        # Compatibility roles for archived strategy tests and old ledgers.
        # The operational service no longer instantiates these strategies.
        "MICRO_REVERSAL_PROBE": "PROBE",
        "PRE_EXPLOSION_REVERSAL": "OBSERVE",
        "OI_MOMENTUM_EARLY": "CONFIRM",
        "OI_EXPANSION_CONFIRMATION": "CONFIRM",
        "OI_MOMENTUM_PULLBACK": "CONFIRM",
        "POST_SQUEEZE_REVERSAL": "REVERSAL",
    }

    def __init__(self, settings: dict[str, Any]):
        self.settings = settings
        self.roles = {**self.DEFAULT_ROLES, **(settings.get("strategy_roles") or {})}

    def role(self, setup: str) -> str:
        return str(self.roles.get(setup, "PROBE")).upper()

    def full_runner_enabled(self, signal: dict[str, Any]) -> bool:
        setups = {str(value) for value in self.settings.get("full_position_runner_setups", [])}
        return str(signal.get("setup") or "") in setups

    def profit_ladder_enabled(self, signal: dict[str, Any]) -> bool:
        setups = {str(value) for value in self.settings.get("full_position_profit_ladder_setups", [])}
        return str(signal.get("setup") or "") in setups

    def minimum_net_lock_pct(self, setup: str) -> float:
        return float((self.settings.get("minimum_net_profit_lock_pct_overrides") or {}).get(
            setup, self.settings.get("minimum_net_profit_lock_pct", 0.0),
        ))

    def prepare(self, signal: dict[str, Any], now_ms: int | None = None) -> dict[str, Any] | None:
        prepared = dict(signal)
        setup = str(signal.get("setup") or "")
        role = self.role(setup)
        entry = float(signal["entry_price"])
        stop = float(signal["stop_price"])
        long = signal["direction"] == "LONG"
        raw_stop_pct = abs(entry - stop) / entry * 100.0 if entry else 0.0
        min_stop_pct = float((self.settings.get("strategy_min_stop_pct_overrides") or {}).get(
            setup, self.settings.get(f"{role.lower()}_min_stop_pct", 0.55),
        ))
        max_stop_pct = float((self.settings.get("strategy_max_stop_pct_overrides") or {}).get(
            setup, self.settings.get(f"{role.lower()}_max_stop_pct", 1.20),
        ))
        if raw_stop_pct <= 0 or raw_stop_pct > max_stop_pct + 1e-9:
            return None
        stop_pct = max(raw_stop_pct, min_stop_pct)
        risk = entry * stop_pct / 100.0
        stop = entry - risk if long else entry + risk
        target_r = float((self.settings.get("strategy_target_r_overrides") or {}).get(
            setup, self.settings.get(f"{role.lower()}_target_r", 2.0),
        ))
        preserve_native_targets = {
            str(value) for value in self.settings.get("preserve_native_target_setups", [])
        }
        if setup in preserve_native_targets:
            target = float(signal["target_price"])
        else:
            target = entry + risk * target_r if long else entry - risk * target_r
        risk_budget = float((self.settings.get("strategy_risk_overrides") or {}).get(
            setup, self.settings.get(f"{role.lower()}_account_risk_pct", 0.10),
        ))
        timestamp_ms = int(now_ms or signal.get("timestamp_ms") or 0)
        prepared.update({
            "timestamp_ms": timestamp_ms,
            "stop_price": stop,
            "target_price": target,
            "risk_pct": stop_pct,
            "campaign_action": role,
            "campaign_id": str(signal.get("campaign_id") or f"{signal['symbol']}-{timestamp_ms}"),
            "account_risk_pct": risk_budget,
            "original_stop_price": float(signal["stop_price"]),
            "original_target_price": float(signal["target_price"]),
            "full_position_runner": self.full_runner_enabled(signal),
            "management_revision": str(self.settings.get("management_revision") or ""),
        })
        return prepared

    def decide(self, signal: dict[str, Any], open_execution: dict[str, Any] | None) -> CampaignDecision:
        setup = str(signal.get("setup") or "")
        role = self.role(setup)
        if role == "OBSERVE":
            return CampaignDecision("HOLD", "strategy_observation_only")
        if open_execution is None and role == "CONFIRM" and not bool(
            self.settings.get("allow_confirm_entry_while_flat", False)
        ):
            return CampaignDecision("HOLD", "confirmation_without_probe")
        prepared = self.prepare(signal)
        if prepared is None:
            return CampaignDecision("REJECT", "campaign_stop_outside_limits")
        role = str(prepared["campaign_action"])
        if open_execution is None:
            return CampaignDecision("ENTER", f"{role.lower()}_while_flat", prepared)
        current_signal = open_execution.get("signal") or {}
        same_direction = current_signal.get("direction") == prepared.get("direction")
        if same_direction:
            current_role = str(current_signal.get("campaign_action") or "PROBE")
            if role == "CONFIRM" and current_role == "PROBE":
                prepared["campaign_id"] = current_signal.get("campaign_id") or prepared["campaign_id"]
                return CampaignDecision("CONFIRM", "probe_confirmed_without_chasing", prepared)
            return CampaignDecision("HOLD", "campaign_already_in_direction", prepared)
        if role == "REVERSAL":
            current_result = open_execution.get("execution") or {}
            # Once the native target has armed a profitable runner, its trailing
            # stop owns the exit.  A fresh opposite scalp signal must not close
            # the protected trade and immediately reopen against it.
            if bool(current_result.get("full_runner_armed")) and bool(
                self.settings.get("protect_armed_runner_from_signal_flip", True)
            ):
                return CampaignDecision("HOLD", "armed_runner_owns_exit", prepared)
            flip_count = int(
                current_result.get("campaign_flip_count")
                or current_signal.get("campaign_flip_count") or 0
            )
            max_flips = int(self.settings.get("max_flips_per_campaign", 1))
            age_minutes = max(
                0.0,
                (int(signal.get("timestamp_ms") or 0) - int(open_execution.get("timestamp_ms") or 0)) / 60_000.0,
            )
            confirmations = (signal.get("evidence") or {}).get("confirmation_checks") or {}
            reversal_score = int((signal.get("evidence") or {}).get("reversal_confirmation_score") or 0)
            scalp_reversal_confirmed = bool(
                setup == "VOLATILITY_EXHAUSTION_FADE_SCALP_V1"
                and confirmations.get("late_candle_confirmation")
                and confirmations.get("directional_exhaustion")
                and confirmations.get("body_expansion")
                and confirmations.get("rejection_wick")
                and confirmations.get("rejection_structure_intact")
                and confirmations.get("taker_flips")
                and reversal_score >= int(self.settings.get("signal_flip_min_reversal_score", 3))
            )
            structure_confirmed = bool(
                (
                    confirmations.get("structure_reverses")
                    and confirmations.get("price_reversal")
                    and confirmations.get("taker_flips")
                )
                or (
                    confirmations.get("structure_holds")
                    and confirmations.get("oi_deterioration_persists")
                    and confirmations.get("taker_holds")
                )
                or (
                    setup == "DUMP_REVERSAL_LONG"
                    and confirmations.get("second_structure_reclaim")
                    and confirmations.get("pullback_floor_holds")
                    and confirmations.get("flow_score")
                )
                or (
                    setup == "DUMP_CONTINUATION_SHORT"
                    and confirmations.get("rejection_structure")
                    and confirmations.get("flow_score")
                    and confirmations.get("live_entry_not_chased")
                )
                or scalp_reversal_confirmed
            )
            if flip_count >= max_flips:
                return CampaignDecision("HOLD", "campaign_flip_limit_reached", prepared)
            if age_minutes < float(self.settings.get("signal_flip_min_age_minutes", 5)):
                return CampaignDecision("HOLD", "opposite_reversal_too_soon", prepared)
            # An opposite signal is not, by itself, permission to kill a healthy
            # position.  Require the live execution to have actually lost part
            # of its original risk before accepting a structural reversal.
            current_r = float(current_result.get("current_r") or 0.0)
            max_flip_r = float(self.settings.get("signal_flip_max_current_r", -0.25))
            if current_r > max_flip_r:
                return CampaignDecision("HOLD", "opposite_signal_while_trade_viable", prepared)
            if not structure_confirmed:
                return CampaignDecision("HOLD", "opposite_reversal_without_structure", prepared)
            prepared["campaign_id"] = current_signal.get("campaign_id") or prepared["campaign_id"]
            prepared["campaign_flip_count"] = flip_count + 1
            return CampaignDecision("FLIP", "opposite_reversal_confirmed", prepared)
        return CampaignDecision("HOLD", "opposite_probe_not_strong_enough", prepared)

    def runner_stop(
        self, execution: dict[str, Any], mark_price: float, market: dict[str, Any] | None,
    ) -> float:
        """Keep the runner behind confirmed 5m structure without widening initial risk."""
        signal, result = execution["signal"], execution["execution"]
        long = signal["direction"] == "LONG"
        initial_stop = float(
            result.get("initial_stop_price") or signal.get("stop_price") or result.get("stop_price") or 0.0
        )
        if not market or initial_stop <= 0 or mark_price <= 0:
            return initial_stop
        lows = [float(market.get(key) or 0.0) for key in ("candle_low", "previous_candle_low")]
        highs = [float(market.get(key) or 0.0) for key in ("candle_high", "previous_candle_high")]
        buffer = float(self.settings.get("runner_structure_buffer_pct", 0.10)) / 100.0
        min_gap = float(self.settings.get("runner_min_mark_gap_pct", 0.25)) / 100.0
        entry = float(result.get("fill_price") or signal.get("entry_price") or 0.0)
        risk = float(result.get("initial_risk_distance") or abs(entry - initial_stop))
        lock_r = float(self.settings.get("runner_after_partial_lock_r", 0.10))
        profit_lock = entry + risk * lock_r if long else entry - risk * lock_r
        if long:
            valid = [value for value in lows if value > 0]
            structural = min(valid) * (1.0 - buffer) if valid else initial_stop
            candidate = max(initial_stop, structural, profit_lock)
            return min(candidate, mark_price * (1.0 - min_gap))
        valid = [value for value in highs if value > 0]
        structural = max(valid) * (1.0 + buffer) if valid else initial_stop
        candidate = min(initial_stop, structural, profit_lock)
        return max(candidate, mark_price * (1.0 + min_gap))

    def progressive_stop(self, execution: dict[str, Any], current_r: float) -> tuple[float, float] | None:
        """Return a fee-aware profit lock after the first payoff, without widening risk."""
        signal, result = execution["signal"], execution["execution"]
        if not result.get("partial_taken"):
            return None
        entry = float(result.get("fill_price") or signal.get("entry_price") or 0.0)
        initial_stop = float(result.get("initial_stop_price") or signal.get("stop_price") or 0.0)
        risk = float(result.get("initial_risk_distance") or abs(entry - initial_stop))
        if entry <= 0 or risk <= 0:
            return None
        mfe = float(result.get("max_mfe_r") or 0.0)
        tiers = [(float(self.settings.get("runner_after_partial_lock_r", 0.10)), 0.0)]
        if mfe >= float(self.settings.get("lock_1_trigger_r", 1.5)):
            tiers.append((float(self.settings.get("lock_1_profit_r", 0.30)), 1.0))
        if mfe >= float(self.settings.get("lock_2_trigger_r", 2.0)):
            tiers.append((float(self.settings.get("lock_2_profit_r", 1.0)), 2.0))
        safety_gap_r = float(self.settings.get("profit_lock_min_gap_r", 0.05))
        viable = [lock for lock, _ in tiers if current_r > lock + safety_gap_r]
        if not viable:
            return None
        lock_r = max(viable)
        long = signal["direction"] == "LONG"
        desired = entry + risk * lock_r if long else entry - risk * lock_r
        current_stop = float(result.get("stop_price") or initial_stop)
        improves = desired > current_stop if long else desired < current_stop
        already_locked = float(result.get("profit_lock_r") or -999.0) >= lock_r
        return (desired, lock_r) if improves and not already_locked else None

    def full_runner_stop(
        self, execution: dict[str, Any], mark_price: float,
        market: dict[str, Any] | None, max_mfe_r: float,
    ) -> tuple[float, float] | None:
        """Trail the entire position after its native target, with no partial realization."""
        signal, result = execution["signal"], execution["execution"]
        entry = float(result.get("fill_price") or signal.get("entry_price") or 0.0)
        initial_stop = float(result.get("initial_stop_price") or signal.get("stop_price") or 0.0)
        risk = float(result.get("initial_risk_distance") or abs(entry - initial_stop))
        if entry <= 0 or risk <= 0 or mark_price <= 0:
            return None
        long = str(signal.get("direction")) == "LONG"
        setup = str(signal.get("setup") or "")
        risk_pct = risk / entry * 100.0
        atr_pct = float((market or {}).get("atr14_pct") or 0.0)
        min_giveback = float((self.settings.get("full_runner_min_giveback_pct_overrides") or {}).get(
            setup, self.settings.get("full_runner_min_giveback_pct", 0.65),
        ))
        max_giveback = float((self.settings.get("full_runner_max_giveback_pct_overrides") or {}).get(
            setup, self.settings.get("full_runner_max_giveback_pct", 1.40),
        ))
        atr_multiple = float((self.settings.get("full_runner_atr_giveback_multiple_overrides") or {}).get(
            setup, self.settings.get("full_runner_atr_giveback_multiple", 1.25),
        ))
        giveback_pct = max(
            min_giveback, min(max_giveback, atr_pct * atr_multiple),
        )
        peak_pct = max_mfe_r * risk_pct
        lock_pct_override = (self.settings.get("full_runner_activation_lock_pct_overrides") or {}).get(setup)
        activation_lock_pct = (
            float(lock_pct_override) if lock_pct_override is not None
            else float(self.settings.get("full_runner_activation_lock_r", 0.50)) * risk_pct
        )
        lock_pct = max(activation_lock_pct, peak_pct - giveback_pct)
        # ATR controls how much the runner may breathe, while these milestones
        # guarantee that Stop Gain advances after a material new price peak.
        ladders = self.settings.get("full_runner_profit_lock_ladders") or {}
        setup_ladder = ladders.get(setup) or ladders.get("*") or []
        reached_locks = [
            float(tier.get("lock_pct", 0.0))
            for tier in setup_ladder
            if peak_pct >= float(tier.get("trigger_pct", 999999.0))
        ]
        if reached_locks:
            lock_pct = max(lock_pct, max(reached_locks))
        # A protected runner must remain profitable after the estimated round-trip
        # fees, spread and a small exit-book buffer.
        lock_pct = max(lock_pct, self.minimum_net_lock_pct(setup))
        desired = entry * (1.0 + lock_pct / 100.0) if long else entry * (1.0 - lock_pct / 100.0)

        structure_disabled = {
            str(value) for value in self.settings.get("full_runner_structure_disabled_setups", [])
        }
        if market and setup not in structure_disabled:
            buffer = float(self.settings.get("runner_structure_buffer_pct", 0.10)) / 100.0
            if long:
                lows = [float(market.get(key) or 0.0) for key in ("candle_low", "previous_candle_low")]
                valid = [value for value in lows if value > entry]
                if valid:
                    desired = max(desired, min(valid) * (1.0 - buffer))
            else:
                highs = [float(market.get(key) or 0.0) for key in ("candle_high", "previous_candle_high")]
                valid = [value for value in highs if 0 < value < entry]
                if valid:
                    desired = min(desired, max(valid) * (1.0 + buffer))

        min_gap = float((self.settings.get("runner_min_price_gap_pct_overrides") or {}).get(
            setup, self.settings.get("runner_min_mark_gap_pct", 0.25),
        )) / 100.0
        desired = min(desired, mark_price * (1.0 - min_gap)) if long else max(
            desired, mark_price * (1.0 + min_gap)
        )
        current_stop = float(result.get("stop_price") or initial_stop)
        improves = desired > current_stop if long else desired < current_stop
        if not improves or (long and desired >= mark_price) or (not long and desired <= mark_price):
            return None
        lock_r = (desired - entry) / risk if long else (entry - desired) / risk
        return desired, lock_r

    def full_position_profit_ladder(
        self, execution: dict[str, Any], current_r: float, max_mfe_r: float,
    ) -> tuple[float, float] | None:
        """Protect the whole position as payoff advances while retaining its fixed target."""
        signal, result = execution["signal"], execution["execution"]
        tiers = [
            (
                float(self.settings.get("profit_ladder_1_trigger_r", 1.0)),
                float(self.settings.get("profit_ladder_1_lock_r", 0.10)),
            ),
            (
                float(self.settings.get("profit_ladder_2_trigger_r", 1.5)),
                float(self.settings.get("profit_ladder_2_lock_r", 0.50)),
            ),
        ]
        reached = [lock for trigger, lock in tiers if max_mfe_r >= trigger]
        if not reached:
            return None
        lock_r = max(reached)
        min_gap_r = float(self.settings.get("profit_ladder_min_mark_gap_r", 0.08))
        if current_r <= lock_r + min_gap_r:
            return None
        entry = float(result.get("fill_price") or signal.get("entry_price") or 0.0)
        initial_stop = float(result.get("initial_stop_price") or signal.get("stop_price") or 0.0)
        risk = float(result.get("initial_risk_distance") or abs(entry - initial_stop))
        if entry <= 0 or risk <= 0:
            return None
        long = str(signal.get("direction")) == "LONG"
        desired = entry + risk * lock_r if long else entry - risk * lock_r
        current_stop = float(result.get("stop_price") or initial_stop)
        improves = desired > current_stop if long else desired < current_stop
        already_locked = float(result.get("profit_lock_r") or -999.0) >= lock_r
        return (desired, lock_r) if improves and not already_locked else None

    def strategy_early_profit_protection(
        self, execution: dict[str, Any], mark_price: float, max_mfe_r: float,
    ) -> tuple[float, float] | None:
        """Lock the entire position after a setup-specific minimum favorable move."""
        signal, result = execution["signal"], execution["execution"]
        setup = str(signal.get("setup") or "")
        cfg = (self.settings.get("strategy_early_profit_protection") or {}).get(setup) or {}
        if not cfg:
            return None
        entry = float(result.get("fill_price") or signal.get("entry_price") or 0.0)
        initial_stop = float(result.get("initial_stop_price") or signal.get("stop_price") or 0.0)
        risk = float(result.get("initial_risk_distance") or abs(entry - initial_stop))
        if entry <= 0 or risk <= 0 or mark_price <= 0:
            return None
        max_mfe_pct = max_mfe_r * risk / entry * 100.0
        if max_mfe_pct < float(cfg.get("trigger_pct", 0.80)):
            return None
        lock_pct = max(
            float(cfg.get("lock_pct", 0.15)), self.minimum_net_lock_pct(setup),
        )
        long = str(signal.get("direction")) == "LONG"
        desired = entry * (1.0 + lock_pct / 100.0) if long else entry * (1.0 - lock_pct / 100.0)
        min_gap = float(self.settings.get("runner_min_mark_gap_pct", 0.25)) / 100.0
        if (long and desired >= mark_price * (1.0 - min_gap)) or (
            not long and desired <= mark_price * (1.0 + min_gap)
        ):
            return None
        current_stop = float(result.get("stop_price") or initial_stop)
        improves = desired > current_stop if long else desired < current_stop
        if not improves:
            return None
        lock_r = (desired - entry) / risk if long else (entry - desired) / risk
        return desired, lock_r

    def manage(
        self, execution: dict[str, Any], mark_price: float, now_ms: int,
        market: dict[str, Any] | None = None,
    ) -> CampaignDecision:
        signal, result = execution["signal"], execution["execution"]
        entry = float(result.get("fill_price") or signal["entry_price"])
        initial_stop = float(
            result.get("initial_stop_price") or signal.get("stop_price") or result.get("stop_price") or 0.0
        )
        risk = float(result.get("initial_risk_distance") or abs(entry - initial_stop))
        if risk <= 0 or mark_price <= 0:
            return CampaignDecision("HOLD", "invalid_live_geometry")
        sign = 1.0 if signal["direction"] == "LONG" else -1.0
        current_r = (mark_price - entry) * sign / risk
        max_mfe_r = max(float(result.get("max_mfe_r") or 0.0), current_r)
        result["max_mfe_r"] = max_mfe_r
        result["current_r"] = current_r
        full_runner = self.full_runner_enabled(signal) or bool(result.get("full_position_runner"))
        if full_runner:
            if not bool(result.get("runner_target_disabled")):
                return CampaignDecision("ENABLE_RUNNER", "full_position_runner_enabled")
            target = float(result.get("initial_target_price") or result.get("target_price") or signal["target_price"])
            activation_r = abs(target - entry) / risk
            if max_mfe_r >= activation_r:
                protection = self.full_runner_stop(execution, mark_price, market, max_mfe_r)
                if protection is not None:
                    stop_price, lock_r = protection
                    return CampaignDecision(
                        "ARM_RUNNER" if not result.get("full_runner_armed") else "PROTECT",
                        "native_target_reached_full_position_trailing",
                        {"stop_price": stop_price, "lock_r": lock_r, "activation_r": activation_r},
                    )
        early_protection = self.strategy_early_profit_protection(
            execution, mark_price, max_mfe_r,
        )
        if early_protection is not None:
            stop_price, lock_r = early_protection
            return CampaignDecision(
                "PROTECT", "strategy_early_profit_protection",
                {"stop_price": stop_price, "lock_r": lock_r},
            )
        if self.profit_ladder_enabled(signal):
            ladder = self.full_position_profit_ladder(execution, current_r, max_mfe_r)
            if ladder is not None:
                stop_price, lock_r = ladder
                return CampaignDecision(
                    "PROTECT", "full_position_profit_ladder",
                    {"stop_price": stop_price, "lock_r": lock_r},
                )
        age_minutes = max(0.0, (now_ms - int(execution["timestamp_ms"])) / 60_000.0)
        max_holding_minutes = float(self.settings.get("max_holding_minutes", 0.0))
        if max_holding_minutes > 0 and age_minutes >= max_holding_minutes:
            return CampaignDecision("EXIT", "campaign_max_holding_time", {
                "age_minutes": age_minutes, "current_r": current_r, "max_mfe_r": max_mfe_r,
            })
        if not full_runner and not result.get("partial_taken") and current_r >= float(self.settings.get("partial_at_r", 0.80)):
            return CampaignDecision("PARTIAL", "campaign_first_payoff")
        protection = self.progressive_stop(execution, current_r)
        if protection is not None:
            stop_price, lock_r = protection
            return CampaignDecision("PROTECT", "campaign_profit_ladder", {
                "stop_price": stop_price, "lock_r": lock_r,
            })
        if not market:
            return CampaignDecision("HOLD", "campaign_active_without_fresh_thesis")
        long = signal["direction"] == "LONG"
        sign = 1.0 if long else -1.0
        role = str(signal.get("campaign_action") or "PROBE")
        taker = float(market.get("taker_buy_sell_ratio") or 1.0)
        price_15m = float(market.get("price_change_15m_pct") or 0.0) * sign
        oi_5m = float(market.get("oi_change_5m_pct") or 0.0)
        entry_market = (signal.get("evidence") or {}).get("entry") or (signal.get("evidence") or {}).get("arm") or {}
        entry_oi_15m = float(entry_market.get("oi_change_15m_pct") or 0.0)
        current_oi_15m = float(market.get("oi_change_15m_pct") or 0.0)
        failures = []
        if price_15m <= -float(self.settings.get("thesis_price_against_pct", 0.10)):
            failures.append("price_against")
        flow_against = (
            taker <= float(self.settings.get("thesis_long_taker_against", 0.95))
            if long else taker >= float(self.settings.get("thesis_short_taker_against", 1.05))
        )
        if flow_against:
            failures.append("taker_against")
        if role == "REVERSAL":
            if oi_5m >= float(self.settings.get("thesis_reversal_oi_continues_pct", 0.08)):
                failures.append("oi_expansion_continues")
        elif (
            oi_5m <= -float(self.settings.get("thesis_continuation_oi_contraction_pct", 0.03))
            or current_oi_15m <= entry_oi_15m - float(self.settings.get("thesis_oi_deterioration_points", 0.15))
        ):
            failures.append("oi_deteriorates")
        result["thesis_failures"] = failures
        setup = str(signal.get("setup") or "")
        price_failure_cfg = (self.settings.get("price_failure_exit") or {}).get(setup) or {}
        if price_failure_cfg:
            failure_age = age_minutes >= float(price_failure_cfg.get("min_age_minutes", 2.0))
            failure_no_progress = max_mfe_r <= float(price_failure_cfg.get("max_mfe_r", 0.30))
            failure_price = current_r <= float(price_failure_cfg.get("max_current_r", -0.55))
            if failure_age and failure_no_progress and failure_price:
                return CampaignDecision("EXIT", "price_failed_without_followthrough", {
                    "age_minutes": age_minutes, "current_r": current_r,
                    "max_mfe_r": max_mfe_r, "failures": failures,
                })
        progress_cfg = (self.settings.get("failed_progress_exit") or {}).get(setup) or {}
        if progress_cfg:
            progress_age = age_minutes >= float(progress_cfg.get("min_age_minutes", 3.0))
            progress_seen = max_mfe_r >= float(progress_cfg.get("min_mfe_r", 0.30))
            progress_below_runner = max_mfe_r < float(progress_cfg.get("max_mfe_r", 0.95))
            progress_given_back = current_r <= float(progress_cfg.get("max_current_r", -0.10))
            progress_failures = len(failures) >= int(progress_cfg.get("min_failures", 2))
            if (
                progress_age and progress_seen and progress_below_runner
                and progress_given_back and progress_failures
            ):
                return CampaignDecision("EXIT", "progress_failed_after_giveback", {
                    "age_minutes": age_minutes, "current_r": current_r,
                    "max_mfe_r": max_mfe_r, "failures": failures,
                })
        launch_cfg = (self.settings.get("failure_to_launch") or {}).get(setup) or {}
        if launch_cfg:
            launch_age = age_minutes >= float(launch_cfg.get("min_age_minutes", 3.0))
            launch_no_progress = max_mfe_r < float(launch_cfg.get("max_mfe_r", 0.40))
            launch_losing = current_r <= float(launch_cfg.get("max_current_r", -0.20))
            launch_failures = len(failures) >= int(launch_cfg.get("min_failures", 2))
            if launch_age and launch_no_progress and launch_losing and launch_failures:
                return CampaignDecision("EXIT", "reversal_failed_to_launch", {
                    "age_minutes": age_minutes, "current_r": current_r,
                    "max_mfe_r": max_mfe_r, "failures": failures,
                })
        opposite_micro_close = (
            float(market.get("micro_close") or 0.0) < float(market.get("micro_open") or 0.0)
            and float(market.get("micro_close") or 0.0) < float(market.get("micro_previous_low") or 0.0)
            if long else
            float(market.get("micro_close") or 0.0) > float(market.get("micro_open") or 0.0)
            and float(market.get("micro_close") or 0.0) > float(market.get("micro_previous_high") or 0.0)
        )
        flip_failures = {"price_against", "taker_against"}.issubset(failures) and len(failures) >= 3
        flip_age = age_minutes >= float(self.settings.get("flip_min_age_minutes", 3))
        flip_price_ok = current_r <= float(self.settings.get("flip_max_current_r", 0.30))
        automatic_flip = bool(self.settings.get("automatic_thesis_flip_enabled", False))
        flip_count = int(result.get("campaign_flip_count") or signal.get("campaign_flip_count") or 0)
        if (
            automatic_flip and flip_count < int(self.settings.get("max_flips_per_campaign", 1))
            and flip_age and flip_price_ok and flip_failures and opposite_micro_close
        ):
            opposite = "SHORT" if long else "LONG"
            flip_entry = float(market.get("price") or mark_price)
            stop_pct = float(self.settings.get("reversal_min_stop_pct", 0.65))
            flip_risk = flip_entry * stop_pct / 100.0
            target_r = float(self.settings.get("reversal_target_r", 2.10))
            flip_stop = flip_entry + flip_risk if opposite == "SHORT" else flip_entry - flip_risk
            flip_target = flip_entry - flip_risk * target_r if opposite == "SHORT" else flip_entry + flip_risk * target_r
            flip_signal = {
                "symbol": signal["symbol"], "direction": opposite, "timestamp_ms": now_ms,
                "entry_price": flip_entry, "stop_price": flip_stop, "target_price": flip_target,
                "risk_pct": stop_pct, "setup": "CAMPAIGN_THESIS_REVERSAL",
                "campaign_action": "REVERSAL", "campaign_id": signal.get("campaign_id"),
                "account_risk_pct": float(self.settings.get("reversal_account_risk_pct", 0.15)),
                "campaign_flip_count": flip_count + 1,
                "evidence": {
                    "entry": market, "reversed_from": signal["direction"],
                    "reversal_failures": failures, "source_execution_id": execution.get("id"),
                },
            }
            return CampaignDecision("FLIP", "opposite_thesis_confirmed", flip_signal)
        review_minutes = float(self.settings.get("thesis_review_minutes", 15))
        no_progress = max_mfe_r < float(self.settings.get("thesis_review_min_mfe_r", 0.35))
        losing_or_flat = current_r <= float(self.settings.get("thesis_review_max_current_r", 0.0))
        if (
            age_minutes >= review_minutes and no_progress and losing_or_flat
            and len(failures) >= int(self.settings.get("thesis_review_min_failures", 2))
        ):
            return CampaignDecision("EXIT", "thesis_invalidated", {"failures": failures})
        hard_review = float(self.settings.get("hard_review_minutes", 60))
        materially_losing = current_r <= float(self.settings.get("hard_review_max_current_r", -0.15))
        if age_minutes >= hard_review and materially_losing and failures:
            return CampaignDecision("EXIT", "extended_thesis_invalidated", {"failures": failures})
        return CampaignDecision("HOLD", "campaign_active")
