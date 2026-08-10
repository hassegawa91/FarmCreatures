from __future__ import annotations

from typing import Any

from engine.binance import MarketSnapshot
from engine.strategy import StrategyEvent


class VolatilityExhaustionFadeScalpStrategy:
    """High-frequency exhaustion fade restricted to late 5m-candle confirmation."""

    name = "VOLATILITY_EXHAUSTION_FADE_SCALP_V1"
    setup = name
    continuation_setup = "VOLATILITY_EXHAUSTION_CONTINUATION_SCALP_V1"

    def __init__(self, settings: dict[str, Any]):
        self.settings = settings
        self.states: dict[str, Any] = {}
        self.last_signal_candle: dict[str, int] = {}
        self.fade_confirmation_pending: dict[str, dict[str, Any]] = {}
        self.deferred_fades: dict[str, dict[str, Any]] = {}
        self.market_context: dict[str, Any] = {}

    def set_market_context(self, context: dict[str, Any] | None) -> None:
        """Provide a production-market benchmark without coupling the strategy to I/O."""
        self.market_context = dict(context or {})

    def prefilter_context(self, context: dict[str, Any]) -> bool:
        """Select scalp candidates cheaply before requesting derivatives data."""
        cfg = self.settings.get("volatility_fade_scalp") or {}
        if not bool(cfg.get("enabled", False)):
            return False
        opened = float(context.get("candle_open") or 0.0)
        closed = float(context.get("candle_close") or 0.0)
        high = float(context.get("candle_high") or 0.0)
        low = float(context.get("candle_low") or 0.0)
        if opened <= 0 or high <= low or closed == opened:
            return False
        body_pct = abs(closed / opened - 1.0) * 100.0
        green = closed > opened
        rejection = (high - closed) / (high - low) if green else (closed - low) / (high - low)
        return (
            int(context.get("directional_candle_count") or 0)
            >= int(cfg.get("min_directional_candles", 2))
            and body_pct >= float(cfg.get("min_body_pct", 0.40))
            and rejection >= float(cfg.get("min_rejection_wick_fraction", 0.15))
        )

    def _checks(self, snapshot: MarketSnapshot) -> dict[str, bool]:
        cfg = self.settings.get("volatility_fade_scalp") or {}
        opened, closed = float(snapshot.candle_open), float(snapshot.candle_close)
        high, low = float(snapshot.candle_high), float(snapshot.candle_low)
        body_pct = abs(closed / opened - 1.0) * 100.0 if opened > 0 else 0.0
        green = closed > opened
        rejection = 0.0
        if high > low:
            rejection = (high - closed) / (high - low) if green else (closed - low) / (high - low)
        direction = "SHORT" if green else "LONG"
        rejection_break_pct = float(cfg.get("max_rejection_break_pct", 0.10))
        rejection_structure_intact = (
            float(snapshot.price) <= high * (1.0 + rejection_break_pct / 100.0)
            if direction == "SHORT" else
            float(snapshot.price) >= low * (1.0 - rejection_break_pct / 100.0)
        )
        candle_age_seconds = max(
            0.0, (int(snapshot.timestamp_ms) - int(snapshot.candle_open_time_ms)) / 1000.0,
        )
        return {
            "enabled": bool(cfg.get("enabled", False)),
            "late_candle_confirmation": candle_age_seconds >= float(
                cfg.get("min_candle_age_seconds", 240)
            ),
            "directional_exhaustion": int(snapshot.directional_candle_count) >= int(
                cfg.get("min_directional_candles", 2)
            ),
            "body_expansion": body_pct >= float(cfg.get("min_body_pct", 0.40)),
            "rejection_wick": rejection >= float(cfg.get("min_rejection_wick_fraction", 0.15)),
            "rejection_structure_intact": rejection_structure_intact,
            "spread": float(snapshot.spread_pct) <= float(cfg.get("max_spread_pct", 0.08)),
        }

    def _arm_continuation(
        self,
        snapshot: MarketSnapshot,
        direction: str,
        reversal_score: int,
        confirmations: dict[str, bool],
        reason: str,
    ) -> None:
        """Keep the first arm time so repeated scans cannot postpone confirmation forever."""
        candle_ms = int(snapshot.candle_open_time_ms)
        current = self.states.get(snapshot.symbol)
        if (
            current
            and int(current.get("candle_open_time_ms") or 0) == candle_ms
            and str(current.get("direction") or "") == direction
        ):
            return
        self.states[snapshot.symbol] = {
            "armed_at_ms": int(snapshot.timestamp_ms),
            "candle_open_time_ms": candle_ms,
            "price": float(snapshot.price),
            "direction": direction,
            "reversal_score": reversal_score,
            "reversal_confirmations": confirmations,
            "arm_reason": reason,
        }

    def diagnose(self, snapshot: MarketSnapshot) -> dict[str, Any]:
        checks = self._checks(snapshot)
        direction = "SHORT" if float(snapshot.candle_close) > float(snapshot.candle_open) else "LONG"
        return {
            "strategy": self.name, "symbol": snapshot.symbol, "direction": direction,
            "phase": "SCALP_READY" if all(checks.values()) else "SCANNING",
            "checks": checks, "passed": sum(checks.values()), "total": len(checks),
            "blocked_by": [name for name, passed in checks.items() if not passed],
        }

    def snapshot(self) -> dict[str, Any]:
        return dict(self.states)

    @staticmethod
    def _reversal_confirmations(
        snapshot: MarketSnapshot, direction: str, cfg: dict[str, Any],
    ) -> dict[str, bool]:
        long = direction == "LONG"
        taker_supports_entry = (
            float(snapshot.taker_buy_sell_ratio) >= float(
                cfg.get("reversal_long_min_taker", 1.05)
            ) if long else float(snapshot.taker_buy_sell_ratio) <= float(
                cfg.get("reversal_short_max_taker", 0.95)
            )
        )
        return {
            "taker_flips": taker_supports_entry,
            # OI is not directional by itself.  Count expansion as a reversal
            # vote only while the latest OI sample is not already unwinding.
            # Directional conflict is handled separately by price/taker guards.
            "oi_15m_expands": (
                float(snapshot.oi_change_15m_pct) > 0.0
                and float(snapshot.oi_change_5m_pct) >= 0.0
            ),
            "lsr_turns_with_entry": (
                float(snapshot.lsr_change_15m_pct) > 0.0 if long
                else float(snapshot.lsr_change_15m_pct) < 0.0
            ),
            "ema_slope_turns_with_entry": (
                float(snapshot.ema21_slope_pct) > 0.0 if long
                else float(snapshot.ema21_slope_pct) < 0.0
            ),
        }

    @staticmethod
    def _directional_continuation_conflict(
        snapshot: MarketSnapshot, direction: str, cfg: dict[str, Any],
    ) -> bool:
        """Reject fades while price, meaningful OI and taker still drive the burst."""
        if not bool(cfg.get("directional_conflict_guard_enabled", True)):
            return False
        minimum_impulse = float(cfg.get("directional_conflict_min_impulse_pct", 1.00))
        minimum_oi = float(cfg.get("directional_conflict_min_oi_15m_pct", 0.50))
        impulse = float(snapshot.price_change_15m_pct)
        oi_expansion = float(snapshot.oi_change_15m_pct) >= minimum_oi
        if direction == "SHORT":
            taker_drives_original_move = float(snapshot.taker_buy_sell_ratio) > 1.0
            price_drives_original_move = impulse >= minimum_impulse
        else:
            taker_drives_original_move = float(snapshot.taker_buy_sell_ratio) < 1.0
            price_drives_original_move = impulse <= -minimum_impulse
        return oi_expansion and taker_drives_original_move and price_drives_original_move

    @staticmethod
    def _breakout_trend_conflict(
        snapshot: MarketSnapshot, direction: str, cfg: dict[str, Any],
    ) -> bool:
        """Do not fade an extended breakout while its EMA structure still accelerates."""
        if not bool(cfg.get("breakout_trend_guard_enabled", True)):
            return False
        minimum_impulse = float(cfg.get("breakout_trend_min_impulse_pct", 2.50))
        minimum_extension = float(cfg.get("breakout_trend_min_range_extension_pct", 1.00))
        minimum_ema_slope = float(cfg.get("breakout_trend_min_ema_slope_pct", 0.20))
        impulse = float(snapshot.price_change_15m_pct)
        ema_slope = float(snapshot.ema21_slope_pct)
        if direction == "SHORT":
            prior_high = float(snapshot.prior_high)
            candle_extension = (
                (float(snapshot.candle_high) / prior_high - 1.0) * 100.0
                if prior_high > 0 else 0.0
            )
            range_extension = max(
                -float(snapshot.distance_to_prior_high_pct), candle_extension,
            )
            return (
                impulse >= minimum_impulse
                and range_extension >= minimum_extension
                and ema_slope >= minimum_ema_slope
            )
        prior_low = float(snapshot.prior_low)
        candle_extension = (
            (prior_low / float(snapshot.candle_low) - 1.0) * 100.0
            if prior_low > 0 and float(snapshot.candle_low) > 0 else 0.0
        )
        range_extension = max(
            -float(snapshot.distance_to_prior_low_pct), candle_extension,
        )
        return (
            impulse <= -minimum_impulse
            and range_extension >= minimum_extension
            and ema_slope <= -minimum_ema_slope
        )

    @staticmethod
    def _recent_flow_continuation_conflict(
        snapshot: MarketSnapshot, direction: str, cfg: dict[str, Any],
    ) -> bool:
        """Reject a fade while the latest 5m flow is still pressing the original move."""
        if not bool(cfg.get("recent_flow_guard_enabled", True)):
            return False
        minimum_move = float(cfg.get("recent_flow_min_price_5m_pct", 0.40))
        maximum_oi = float(cfg.get("recent_flow_max_oi_5m_pct", -0.50))
        price_5m = float(snapshot.price_change_5m_pct)
        oi_5m = float(snapshot.oi_change_5m_pct)
        taker = float(snapshot.taker_buy_sell_ratio)
        micro_open = float(snapshot.micro_open)
        micro_close = float(snapshot.micro_close)
        if direction == "LONG":
            return (
                price_5m <= -minimum_move and oi_5m <= maximum_oi
                and taker < 1.0 and micro_close < micro_open
            )
        return (
            price_5m >= minimum_move and oi_5m <= maximum_oi
            and taker > 1.0 and micro_close > micro_open
        )

    @staticmethod
    def _directional_position_build_conflict(
        snapshot: MarketSnapshot, direction: str, cfg: dict[str, Any],
    ) -> bool:
        """Block fades while fresh positions and aggressive flow build a strong trend."""
        if not bool(cfg.get("directional_position_build_guard_enabled", True)):
            return False
        minimum_move = float(cfg.get("directional_position_build_min_price_5m_pct", 0.40))
        minimum_oi_15m = float(cfg.get("directional_position_build_min_oi_15m_pct", 0.50))
        minimum_oi_5m = float(cfg.get("directional_position_build_min_oi_5m_pct", 0.00))
        minimum_adx = float(cfg.get("directional_position_build_min_adx", 35.0))
        if (
            float(snapshot.oi_change_15m_pct) < minimum_oi_15m
            or float(snapshot.oi_change_5m_pct) < minimum_oi_5m
            or float(snapshot.adx14) < minimum_adx
        ):
            return False
        if direction == "LONG":
            return (
                float(snapshot.price_change_5m_pct) <= -minimum_move
                and float(snapshot.taker_buy_sell_ratio)
                <= float(cfg.get("directional_position_build_long_max_taker", 0.80))
            )
        return (
            float(snapshot.price_change_5m_pct) >= minimum_move
            and float(snapshot.taker_buy_sell_ratio)
            >= float(cfg.get("directional_position_build_short_min_taker", 1.20))
        )

    @staticmethod
    def _price_reversal_confirmed(
        snapshot: MarketSnapshot, direction: str, cfg: dict[str, Any],
    ) -> bool:
        """Require price itself to turn; derivatives flow alone is not an entry trigger."""
        minimum = float(cfg.get("reversal_min_directional_price_5m_pct", 0.05))
        long = direction == "LONG"
        price_5m_turn = (
            float(snapshot.price_change_5m_pct) >= minimum if long
            else float(snapshot.price_change_5m_pct) <= -minimum
        )
        micro_structure_turn = (
            float(snapshot.micro_close) > float(snapshot.micro_open)
            and float(snapshot.micro_close) > float(snapshot.micro_previous_high)
            if long else
            float(snapshot.micro_close) < float(snapshot.micro_open)
            and float(snapshot.micro_close) < float(snapshot.micro_previous_low)
        )
        return price_5m_turn or micro_structure_turn

    @staticmethod
    def _extreme_volatility_conflict(
        snapshot: MarketSnapshot, cfg: dict[str, Any],
    ) -> bool:
        """Do not catch liquidation candles whose volatility dwarfs the configured stop."""
        if not bool(cfg.get("extreme_volatility_guard_enabled", False)):
            return False
        return float(snapshot.atr14_pct) > float(cfg.get("max_entry_atr_pct", 6.0))

    def _observe_continuation(
        self, snapshot: MarketSnapshot, cfg: dict[str, Any],
    ) -> StrategyEvent | None:
        arm = self.states.get(snapshot.symbol)
        if not arm or not bool(cfg.get("continuation_enabled", False)):
            return None
        age_seconds = (int(snapshot.timestamp_ms) - int(arm["armed_at_ms"])) / 1000.0
        if age_seconds < float(cfg.get("continuation_min_age_seconds", 45)):
            return None
        if age_seconds > float(cfg.get("continuation_ttl_seconds", 600)):
            self.states.pop(snapshot.symbol, None)
            return None
        direction = str(arm["direction"])
        long = direction == "LONG"
        micro_break = (
            float(snapshot.micro_close) > float(snapshot.micro_open)
            and float(snapshot.micro_close) > float(snapshot.micro_previous_high)
            if long else
            float(snapshot.micro_close) < float(snapshot.micro_open)
            and float(snapshot.micro_close) < float(snapshot.micro_previous_low)
        )
        taker_holds = (
            float(snapshot.taker_buy_sell_ratio) >= float(cfg.get("continuation_long_min_taker", 1.03))
            if long else
            float(snapshot.taker_buy_sell_ratio) <= float(cfg.get("continuation_short_max_taker", 0.97))
        )
        oi_holds = float(snapshot.oi_change_5m_pct) >= float(
            cfg.get("continuation_min_oi_5m_pct", -0.05)
        )
        price_5m_holds = (
            float(snapshot.price_change_5m_pct) >= float(
                cfg.get("continuation_min_directional_price_5m_pct", 0.05)
            ) if long else float(snapshot.price_change_5m_pct) <= -float(
                cfg.get("continuation_min_directional_price_5m_pct", 0.05)
            )
        ) if bool(cfg.get("continuation_require_price_5m_alignment", False)) else True
        oi_15m_holds = float(snapshot.oi_change_15m_pct) >= float(
            cfg.get("continuation_min_oi_15m_pct", -0.10)
        ) if bool(cfg.get("continuation_require_oi_15m_support", False)) else True
        volatility_ok = float(snapshot.atr14_pct) <= float(
            cfg.get("continuation_max_atr_pct", 6.0)
        ) if bool(cfg.get("continuation_volatility_guard_enabled", False)) else True
        reference = float(arm["price"])
        signed_extension = (
            (float(snapshot.price) - reference) / reference * 100.0
            if long else (reference - float(snapshot.price)) / reference * 100.0
        )
        extension_ok = signed_extension <= float(cfg.get("continuation_max_extension_pct", 0.80))
        directional_macro_move = (
            float(snapshot.price_change_15m_pct)
            if long else -float(snapshot.price_change_15m_pct)
        )
        macro_extension_ok = directional_macro_move <= float(
            cfg.get("continuation_max_directional_15m_pct", 2.50)
        )
        volume_growth_ok = float(snapshot.volume_growth_ratio) >= float(
            cfg.get("continuation_min_volume_growth_ratio", 0.75)
        )
        edge = float(snapshot.edge_position)
        edge_ok = (
            edge <= float(cfg.get("continuation_long_max_edge_position", 0.85))
            if long else edge >= float(cfg.get("continuation_short_min_edge_position", 0.15))
        )
        if not (
            micro_break and taker_holds and oi_holds and price_5m_holds
            and oi_15m_holds and volatility_ok and extension_ok
            and macro_extension_ok and volume_growth_ok and edge_ok
        ):
            return None
        adx = float(snapshot.adx14)
        adx_uncertain = (
            adx < float(cfg.get("continuation_second_confirmation_min_adx", 12.0))
            or adx > float(cfg.get("continuation_second_confirmation_max_adx", 60.0))
        )
        benchmark_5m = float(self.market_context.get("price_change_5m_pct") or 0.0)
        benchmark_15m = float(self.market_context.get("price_change_15m_pct") or 0.0)
        benchmark_available = bool(self.market_context)
        benchmark_flat = benchmark_available and (
            abs(benchmark_5m) < float(cfg.get("continuation_benchmark_flat_5m_pct", 0.20))
            and abs(benchmark_15m) < float(cfg.get("continuation_benchmark_flat_15m_pct", 0.30))
        )
        benchmark_opposes = benchmark_available and (
            benchmark_5m < -float(cfg.get("continuation_benchmark_opposite_5m_pct", 0.15))
            if long else benchmark_5m > float(cfg.get("continuation_benchmark_opposite_5m_pct", 0.15))
        )
        require_second = bool(cfg.get("continuation_regime_second_confirmation_enabled", False)) and (
            adx_uncertain or benchmark_flat or benchmark_opposes
        )
        regime_second_confirmed = not require_second
        if require_second:
            pending = arm.get("regime_confirmation") or {}
            current_micro_ms = int(snapshot.micro_open_time_ms)
            fresh_micro = current_micro_ms > int(pending.get("micro_open_time_ms") or 0)
            held_price = (
                float(snapshot.price) >= float(pending.get("price") or snapshot.price)
                if long else float(snapshot.price) <= float(pending.get("price") or snapshot.price)
            )
            regime_second_confirmed = bool(pending and fresh_micro and held_price)
            if not regime_second_confirmed:
                arm["regime_confirmation"] = {
                    "micro_open_time_ms": current_micro_ms,
                    "price": float(snapshot.price),
                    "adx_uncertain": adx_uncertain,
                    "benchmark_flat": benchmark_flat,
                    "benchmark_opposes": benchmark_opposes,
                }
                return None
        entry = float(snapshot.price)
        stop_pct = max(
            float(cfg.get("continuation_stop_pct", 1.50)),
            min(
                float(cfg.get("continuation_max_stop_pct", 2.00)),
                float(snapshot.atr14_pct) * float(cfg.get("continuation_stop_atr_multiple", 0.75)),
            ),
        )
        target_pct = float(cfg.get("continuation_target_pct", 1.00))
        stop = entry * (1.0 - stop_pct / 100.0) if long else entry * (1.0 + stop_pct / 100.0)
        target = entry * (1.0 + target_pct / 100.0) if long else entry * (1.0 - target_pct / 100.0)
        self.states.pop(snapshot.symbol, None)
        self.last_signal_candle[snapshot.symbol] = int(snapshot.candle_open_time_ms)
        payload = {
            "symbol": snapshot.symbol, "direction": direction,
            "timestamp_ms": int(snapshot.timestamp_ms), "entry_price": entry,
            "stop_price": stop, "target_price": target, "risk_pct": stop_pct,
            "setup": self.continuation_setup,
            "evidence": {
                "confirmation_checks": {
                    "micro_rebreak": micro_break, "taker_holds": taker_holds,
                    "oi_holds": oi_holds, "price_5m_aligned": price_5m_holds,
                    "oi_15m_supports": oi_15m_holds, "volatility_ok": volatility_ok,
                    "entry_not_extended": extension_ok,
                    "macro_not_extended": macro_extension_ok,
                    "volume_growth": volume_growth_ok, "edge_not_chased": edge_ok,
                    "regime_second_confirmed": regime_second_confirmed,
                },
                "entry": snapshot.to_dict(), "arm": arm,
                "classification": self.continuation_setup,
                "strategy_version": "SCORE_AND_CONTINUATION_V3",
                "research_model": "EXHAUSTION_REJECTED_CONTINUATION_AFTER_MICRO_REBREAK",
            },
        }
        return StrategyEvent(
            "SIGNAL", snapshot.symbol, direction, int(snapshot.timestamp_ms),
            "volatility_exhaustion_continuation", payload,
        )

    def _fade_event(
        self,
        snapshot: MarketSnapshot,
        direction: str,
        cfg: dict[str, Any],
        evidence: dict[str, Any],
    ) -> StrategyEvent:
        entry = float(snapshot.price)
        stop_pct = float(cfg.get("stop_pct", 2.00))
        if bool(cfg.get("adaptive_stop_enabled", False)):
            atr_stop = float(snapshot.atr14_pct) * float(
                cfg.get("adaptive_stop_atr_multiple", 0.90)
            )
            stop_pct = max(
                stop_pct,
                min(float(cfg.get("adaptive_stop_max_pct", stop_pct)), atr_stop),
            )
        target_pct = float(cfg.get("target_pct", 0.80))
        stop = (
            entry * (1.0 + stop_pct / 100.0)
            if direction == "SHORT" else entry * (1.0 - stop_pct / 100.0)
        )
        target = (
            entry * (1.0 - target_pct / 100.0)
            if direction == "SHORT" else entry * (1.0 + target_pct / 100.0)
        )
        self.last_signal_candle[snapshot.symbol] = int(snapshot.candle_open_time_ms)
        payload = {
            "symbol": snapshot.symbol, "direction": direction,
            "timestamp_ms": int(snapshot.timestamp_ms), "entry_price": entry,
            "stop_price": stop, "target_price": target, "risk_pct": stop_pct,
            "setup": self.setup,
            "evidence": {
                **evidence,
                "entry": snapshot.to_dict(),
                "classification": self.setup,
                "strategy_version": "DEFERRED_REPRICE_V10",
                "research_model": "EXHAUSTION_REPRICED_AFTER_FIXED_DELAY",
                "adaptive_stop_pct": stop_pct,
            },
        }
        return StrategyEvent(
            "SIGNAL", snapshot.symbol, direction, int(snapshot.timestamp_ms),
            "volatility_exhaustion_fade", payload,
        )

    def _observe_deferred_fade(
        self, snapshot: MarketSnapshot, cfg: dict[str, Any],
    ) -> StrategyEvent | None:
        arm = self.deferred_fades.get(snapshot.symbol)
        if not arm:
            return None
        age_seconds = (int(snapshot.timestamp_ms) - int(arm["armed_at_ms"])) / 1000.0
        delay_seconds = float(cfg.get("fade_deferred_confirmation_seconds", 0.0))
        ttl_seconds = float(cfg.get("fade_deferred_confirmation_ttl_seconds", 300.0))
        if age_seconds < delay_seconds:
            return None
        direction = str(arm["direction"])
        confirmations = self._reversal_confirmations(snapshot, direction, cfg)
        reversal_score = sum(confirmations.values())
        price_reversal = self._price_reversal_confirmed(snapshot, direction, cfg)
        directional_conflict = self._directional_continuation_conflict(snapshot, direction, cfg)
        breakout_trend_conflict = self._breakout_trend_conflict(snapshot, direction, cfg)
        recent_flow_conflict = self._recent_flow_continuation_conflict(snapshot, direction, cfg)
        position_build_conflict = self._directional_position_build_conflict(snapshot, direction, cfg)
        extreme_volatility_conflict = self._extreme_volatility_conflict(snapshot, cfg)
        reference = float(arm["price"])
        self.deferred_fades.pop(snapshot.symbol, None)
        if age_seconds > ttl_seconds:
            if bool(cfg.get("continuation_enabled", False)):
                self._arm_continuation(
                    snapshot,
                    "LONG" if direction == "SHORT" else "SHORT",
                    reversal_score,
                    confirmations,
                    "deferred_fade_expired",
                )
            return None
        return self._fade_event(snapshot, direction, cfg, {
            "confirmation_checks": {
                **confirmations,
                "directional_continuation_conflict": directional_conflict,
                "breakout_trend_conflict": breakout_trend_conflict,
                "recent_flow_continuation_conflict": recent_flow_conflict,
                "directional_position_build_conflict": position_build_conflict,
                "extreme_volatility_conflict": extreme_volatility_conflict,
            },
            "reversal_confirmation_score": reversal_score,
            "price_reversal_confirmed": price_reversal,
            "second_reclaim_confirmed": bool(arm.get("second_reclaim_confirmed", True)),
            "deferred_confirmation": {
                "armed_at_ms": int(arm["armed_at_ms"]),
                "repriced_at_ms": int(snapshot.timestamp_ms),
                "age_seconds": age_seconds,
                "reference_price": reference,
                "reprice_drift_pct": (
                    (float(snapshot.price) / reference - 1.0) * 100.0
                    if reference > 0 else 0.0
                ),
                "original_entry": arm.get("original_entry") or {},
            },
        })

    def observe(self, snapshot: MarketSnapshot) -> StrategyEvent | None:
        cfg = self.settings.get("volatility_fade_scalp") or {}
        if snapshot.symbol in self.deferred_fades:
            return self._observe_deferred_fade(snapshot, cfg)
        continuation = self._observe_continuation(snapshot, cfg)
        if continuation is not None:
            return continuation
        candle_ms = int(snapshot.candle_open_time_ms)
        if self.last_signal_candle.get(snapshot.symbol) == candle_ms:
            return None
        checks = self._checks(snapshot)
        direction = "SHORT" if float(snapshot.candle_close) > float(snapshot.candle_open) else "LONG"
        confirmations = self._reversal_confirmations(snapshot, direction, cfg)
        reversal_score = sum(confirmations.values())
        if not all(checks.values()):
            base_checks = {
                name: passed for name, passed in checks.items()
                if name != "rejection_structure_intact"
            }
            if (
                not checks.get("rejection_structure_intact", True)
                and all(base_checks.values())
                and bool(cfg.get("continuation_enabled", False))
            ):
                # The rejected extreme was broken: the fade thesis is invalid, but the
                # same event can become a continuation after a fresh micro rebreak.
                self._arm_continuation(
                    snapshot,
                    "LONG" if direction == "SHORT" else "SHORT",
                    reversal_score,
                    confirmations,
                    "rejection_extreme_broken",
                )
            return None
        directional_conflict = self._directional_continuation_conflict(snapshot, direction, cfg)
        breakout_trend_conflict = self._breakout_trend_conflict(snapshot, direction, cfg)
        recent_flow_conflict = self._recent_flow_continuation_conflict(snapshot, direction, cfg)
        position_build_conflict = self._directional_position_build_conflict(snapshot, direction, cfg)
        extreme_volatility_conflict = self._extreme_volatility_conflict(snapshot, cfg)
        confirmations_with_conflict = {
            **confirmations,
            "directional_continuation_conflict": directional_conflict,
            "breakout_trend_conflict": breakout_trend_conflict,
            "recent_flow_continuation_conflict": recent_flow_conflict,
            "directional_position_build_conflict": position_build_conflict,
            "extreme_volatility_conflict": extreme_volatility_conflict,
        }
        if (
            directional_conflict or breakout_trend_conflict
            or recent_flow_conflict or position_build_conflict or extreme_volatility_conflict
        ):
            if bool(cfg.get("continuation_enabled", False)):
                self._arm_continuation(
                    snapshot,
                    "LONG" if direction == "SHORT" else "SHORT",
                    reversal_score,
                    confirmations_with_conflict,
                    (
                        "breakout_trend_still_intact"
                        if breakout_trend_conflict else "directional_oi_taker_conflict"
                        if directional_conflict else "directional_position_build"
                        if position_build_conflict else "extreme_volatility"
                        if extreme_volatility_conflict else "recent_flow_still_directional"
                    ),
                )
            return None
        minimum_score = int(cfg.get("min_reversal_confirmation_score", 0))
        # OI and LSR are contextual votes, not directional execution flow. A
        # fade backed only by those votes can still oppose active taker flow
        # and EMA slope. Production may require one direct reversal vote
        # without increasing the global score for every setup.
        taker = float(snapshot.taker_buy_sell_ratio)
        taker_neutral = (
            float(cfg.get("ema_confirmation_min_taker", 0.90))
            <= taker <= float(cfg.get("ema_confirmation_max_taker", 1.10))
        )
        ema_direct_allowed = bool(
            confirmations["ema_slope_turns_with_entry"] and taker_neutral
            and float(snapshot.adx14) < float(cfg.get("ema_confirmation_max_adx", 50.0))
        )
        direct_reversal = bool(confirmations["taker_flips"] or ema_direct_allowed)
        price_reversal = self._price_reversal_confirmed(snapshot, direction, cfg)
        direct_required = bool(cfg.get("direct_reversal_confirmation_required", False))
        price_required = bool(cfg.get("direct_price_reversal_required", False))
        if (
            reversal_score < minimum_score
            or (direct_required and not direct_reversal)
            or (price_required and not price_reversal)
        ):
            if bool(cfg.get("continuation_enabled", False)):
                self._arm_continuation(
                    snapshot,
                    "LONG" if direction == "SHORT" else "SHORT",
                    reversal_score,
                    confirmations,
                    (
                        "missing_price_reversal_confirmation"
                        if price_required and not price_reversal
                        else "missing_direct_reversal_confirmation"
                        if direct_required and not direct_reversal
                        else "insufficient_reversal_confirmation"
                    ),
                )
            return None
        oi_second_reclaim = bool(cfg.get("fade_second_reclaim_on_oi_expansion_enabled", False)) and (
            float(snapshot.oi_change_15m_pct) >= float(cfg.get("fade_second_reclaim_min_oi_15m_pct", 0.15))
            and float(snapshot.oi_change_5m_pct) >= float(cfg.get("fade_second_reclaim_min_oi_5m_pct", 0.02))
        )
        second_reclaim_confirmed = not oi_second_reclaim
        if oi_second_reclaim:
            pending = self.fade_confirmation_pending.get(snapshot.symbol) or {}
            current_micro_ms = int(snapshot.micro_open_time_ms)
            fresh_micro = current_micro_ms > int(pending.get("micro_open_time_ms") or 0)
            long = direction == "LONG"
            fresh_structure = (
                float(snapshot.micro_close) > float(snapshot.micro_open)
                and float(snapshot.micro_close) > float(snapshot.micro_previous_high)
                if long else
                float(snapshot.micro_close) < float(snapshot.micro_open)
                and float(snapshot.micro_close) < float(snapshot.micro_previous_low)
            )
            tolerance = float(cfg.get("fade_second_reclaim_price_tolerance_pct", 0.10)) / 100.0
            prior_price = float(pending.get("price") or snapshot.price)
            structure_held = (
                float(snapshot.price) >= prior_price * (1.0 - tolerance)
                if long else float(snapshot.price) <= prior_price * (1.0 + tolerance)
            )
            second_reclaim_confirmed = bool(pending and fresh_micro and fresh_structure and structure_held)
            if not second_reclaim_confirmed:
                self.fade_confirmation_pending[snapshot.symbol] = {
                    "direction": direction, "micro_open_time_ms": current_micro_ms,
                    "price": float(snapshot.price), "timestamp_ms": int(snapshot.timestamp_ms),
                }
                return None
        self.fade_confirmation_pending.pop(snapshot.symbol, None)
        evidence = {
            "confirmation_checks": {**checks, **confirmations_with_conflict},
            "reversal_confirmation_score": reversal_score,
            "price_reversal_confirmed": price_reversal,
            "second_reclaim_confirmed": second_reclaim_confirmed,
        }
        delay_seconds = float(cfg.get("fade_deferred_confirmation_seconds", 0.0))
        if delay_seconds > 0:
            self.deferred_fades[snapshot.symbol] = {
                "armed_at_ms": int(snapshot.timestamp_ms),
                "candle_open_time_ms": candle_ms,
                "price": float(snapshot.price),
                "direction": direction,
                "second_reclaim_confirmed": second_reclaim_confirmed,
                "original_entry": snapshot.to_dict(),
            }
            return None
        return self._fade_event(snapshot, direction, cfg, evidence)
