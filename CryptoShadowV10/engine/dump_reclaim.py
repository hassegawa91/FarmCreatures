from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from engine.binance import MarketSnapshot
from engine.strategy import StrategyEvent, TradeSignal


@dataclass
class DumpReclaimState:
    symbol: str
    armed_at_ms: int
    expires_at_ms: int
    last_dump_candle_ms: int
    last_seen_candle_ms: int
    dump_low: float
    max_dump_pct: float
    arm_snapshot: dict[str, Any]
    phase: str = "WAIT_FIRST_RECLAIM"
    first_reclaim_candle_ms: int = 0
    first_reclaim_high: float = 0.0
    pullback_candle_ms: int = 0
    pullback_low: float = 0.0
    rebound_seen: bool = False
    rebound_candle_ms: int = 0
    rebound_high: float = 0.0
    attempt: int = 1
    source_execution_id: int | None = None


class DumpExhaustionReclaimStrategy:
    """Loose dump detector with mutually exclusive reversal and continuation entries."""

    name = "DUMP_REGIME_V2"
    reversal_setup = "DUMP_REVERSAL_LONG"
    continuation_setup = "DUMP_CONTINUATION_SHORT"

    def __init__(self, settings: dict[str, Any]):
        self.settings = settings
        self.states: dict[str, DumpReclaimState] = {}
        self.last_signal_ms: dict[str, int] = {}

    def _cooldown_active(self, symbol: str, now_ms: int) -> bool:
        last = self.last_signal_ms.get(symbol)
        cooldown_ms = int(float(self.settings.get("dump_reclaim_cooldown_minutes", 60)) * 60_000)
        return last is not None and now_ms - last < cooldown_ms

    def _is_sharp_dump(self, snapshot: MarketSnapshot) -> bool:
        threshold = float(self.settings.get("dump_reclaim_arm_price_5m_pct", 1.5))
        return snapshot.price_change_5m_pct <= -threshold

    @staticmethod
    def _change_pct(current: float, previous: float) -> float:
        return (current - previous) / previous * 100.0 if previous > 0 else 0.0

    def _rebound_pct(self, state: DumpReclaimState, snapshot: MarketSnapshot) -> float:
        return self._change_pct(float(snapshot.candle_high), state.dump_low)

    def _minimum_rebound_pct(self, snapshot: MarketSnapshot) -> float:
        return max(
            float(self.settings.get("dump_continuation_min_rebound_pct", 0.50)),
            float(snapshot.atr14_pct)
            * float(self.settings.get("dump_continuation_min_rebound_atr_fraction", 0.35)),
        )

    def _record_rebound(self, state: DumpReclaimState, snapshot: MarketSnapshot) -> None:
        if self._rebound_pct(state, snapshot) < self._minimum_rebound_pct(snapshot):
            return
        state.rebound_seen = True
        state.rebound_candle_ms = int(snapshot.candle_open_time_ms)
        state.rebound_high = max(state.rebound_high, float(snapshot.candle_high))

    def _first_reclaim_checks(
        self, state: DumpReclaimState, snapshot: MarketSnapshot,
    ) -> dict[str, bool]:
        silence_ms = int(float(self.settings.get("dump_reclaim_silence_minutes", 5)) * 60_000)
        rebound_from_low_pct = self._change_pct(float(snapshot.price), state.dump_low)
        max_rebound_pct = max(
            float(self.settings.get("dump_reclaim_max_rebound_floor_pct", 3.0)),
            float(snapshot.atr14_pct)
            * float(self.settings.get("dump_reclaim_max_rebound_atr_multiple", 1.5)),
        )
        return {
            "causal_silence": snapshot.candle_open_time_ms - state.last_dump_candle_ms >= silence_ms,
            "no_new_dump": not self._is_sharp_dump(snapshot),
            "price_reclaiming": (
                snapshot.candle_close > snapshot.candle_open
                and snapshot.candle_close >= snapshot.previous_candle_close
            ),
            "minimum_rebound": rebound_from_low_pct >= self._minimum_rebound_pct(snapshot),
            "reclaim_not_chased": rebound_from_low_pct <= max_rebound_pct,
            # This is only a broad detector. Direction is decided on the second reclaim.
            "some_flow_response": (
                snapshot.lsr_change_5m_pct < 0
                or snapshot.taker_buy_sell_ratio >= float(
                    self.settings.get("dump_scanner_min_taker_ratio", 0.95)
                )
            ),
            "taker_not_climax": snapshot.taker_buy_sell_ratio <= float(
                self.settings.get("dump_reclaim_max_taker_ratio", 1.60)
            ),
            "spread": snapshot.spread_pct <= float(self.settings.get("max_spread_pct", 0.08)),
        }

    def _pullback_checks(self, state: DumpReclaimState, snapshot: MarketSnapshot) -> dict[str, bool]:
        floor_tolerance = max(
            float(self.settings.get("dump_reclaim_pullback_floor_tolerance_pct", 0.20)),
            float(snapshot.atr14_pct)
            * float(self.settings.get("dump_reclaim_pullback_floor_atr_fraction", 0.10)),
        )
        floor = state.dump_low * (1.0 - floor_tolerance / 100.0)
        return {
            "fresh_after_first_reclaim": int(snapshot.candle_open_time_ms) > state.first_reclaim_candle_ms,
            "real_pullback": (
                float(snapshot.candle_low) < state.first_reclaim_high
                and float(snapshot.candle_close) <= float(snapshot.previous_candle_close)
            ),
            "dump_floor_preserved": float(snapshot.candle_low) >= floor,
            "no_new_dump": not self._is_sharp_dump(snapshot),
        }

    def _reversal_checks(
        self, state: DumpReclaimState, snapshot: MarketSnapshot,
    ) -> dict[str, bool]:
        silence_ms = int(float(self.settings.get("dump_reclaim_silence_minutes", 5)) * 60_000)
        floor_tolerance = max(
            float(self.settings.get("dump_reclaim_second_reclaim_floor_tolerance_pct", 0.20)),
            float(snapshot.atr14_pct)
            * float(self.settings.get("dump_reclaim_second_reclaim_floor_atr_fraction", 0.10)),
        )
        reclaim_from_pullback_pct = self._change_pct(
            float(snapshot.candle_close), state.pullback_low,
        )
        min_reclaim_pct = max(
            float(self.settings.get("dump_reclaim_second_reclaim_min_pct", 0.08)),
            float(snapshot.atr14_pct)
            * float(self.settings.get("dump_reclaim_second_reclaim_min_atr_fraction", 0.15)),
        )
        rebound_from_low_pct = self._change_pct(float(snapshot.price), state.dump_low)
        max_rebound_pct = max(
            float(self.settings.get("dump_reclaim_second_reclaim_max_rebound_floor_pct", 3.0)),
            float(snapshot.atr14_pct)
            * float(self.settings.get("dump_reclaim_second_reclaim_max_rebound_atr_multiple", 1.5)),
        )
        oi_accelerating = snapshot.oi_acceleration_5m_pct_points >= float(
            self.settings.get("dump_reversal_min_oi_acceleration", 0.0)
        )
        lsr_unwinds = snapshot.lsr_change_5m_pct <= float(
            self.settings.get("dump_reversal_max_lsr_5m_pct", 0.0)
        )
        taker_buying = snapshot.taker_buy_sell_ratio >= float(
            self.settings.get("dump_reversal_min_taker_ratio", 1.0)
        )
        flow_votes = sum((oi_accelerating, lsr_unwinds, taker_buying))
        return {
            "causal_silence": int(snapshot.candle_open_time_ms) - state.last_dump_candle_ms >= silence_ms,
            "no_new_dump": not self._is_sharp_dump(snapshot),
            "fresh_after_pullback": int(snapshot.candle_open_time_ms) > state.pullback_candle_ms,
            "second_structure_reclaim": (
                float(snapshot.candle_close) > float(snapshot.previous_candle_close)
                and float(snapshot.candle_close) > float(snapshot.candle_open)
                and reclaim_from_pullback_pct >= min_reclaim_pct
            ),
            "pullback_floor_holds": (
                state.pullback_low > 0
                and float(snapshot.candle_low)
                >= state.pullback_low * (1.0 - floor_tolerance / 100.0)
            ),
            "reclaim_not_chased": rebound_from_low_pct <= max_rebound_pct,
            "oi_building": snapshot.oi_change_15m_pct >= float(
                self.settings.get("dump_reversal_min_oi_15m_pct", 0.0)
            ),
            "flow_score": flow_votes >= int(
                self.settings.get("dump_reversal_min_flow_votes", 2)
            ),
            "taker_not_climax": snapshot.taker_buy_sell_ratio <= float(
                self.settings.get("dump_reclaim_max_taker_ratio", 1.60)
            ),
            "spread": snapshot.spread_pct <= float(self.settings.get("max_spread_pct", 0.08)),
        }

    def _continuation_checks(
        self, state: DumpReclaimState, snapshot: MarketSnapshot,
        prior_dump_low: float | None = None,
    ) -> dict[str, bool]:
        boundary = float(prior_dump_low or state.dump_low)
        break_buffer = float(self.settings.get("dump_continuation_break_buffer_pct", 0.08)) / 100.0
        # The order uses the live snapshot price. Chasing must therefore be measured from
        # that same price, rather than from the older closed candle that confirmed structure.
        extension_pct = max(0.0, -self._change_pct(float(snapshot.price), boundary))
        max_extension = max(
            float(self.settings.get("dump_continuation_max_break_extension_pct", 1.00)),
            float(snapshot.atr14_pct)
            * float(self.settings.get("dump_continuation_max_break_atr_fraction", 0.75)),
        )
        hard_max_extension = float(
            self.settings.get("dump_continuation_hard_max_break_extension_pct", 999.0)
        )
        max_extension = min(max_extension, hard_max_extension)
        bearish_structure = (
            float(snapshot.candle_close) < float(snapshot.candle_open)
            and float(snapshot.candle_close) < float(snapshot.previous_candle_close)
        )
        rejection_structure = bearish_structure and (
            float(snapshot.candle_close) < float(snapshot.previous_candle_low)
            or float(snapshot.candle_close) < boundary * (1.0 - break_buffer)
        )
        seller_taker = snapshot.taker_buy_sell_ratio <= float(
            self.settings.get("dump_continuation_max_taker_ratio", 0.90)
        )
        oi_supports = (
            snapshot.oi_change_15m_pct >= float(
                self.settings.get("dump_continuation_min_oi_15m_pct", -0.10)
            )
            or snapshot.oi_acceleration_5m_pct_points >= float(
                self.settings.get("dump_continuation_min_oi_acceleration", 0.05)
            )
        )
        trades_not_climax = snapshot.trades_ratio <= float(
            self.settings.get("dump_continuation_max_trades_ratio", 1.40)
        )
        flow_votes = sum((seller_taker, oi_supports, trades_not_climax))
        fresh_context = True
        if bool(self.settings.get("dump_continuation_fresh_context_enabled", False)):
            max_age_ms = int(
                float(self.settings.get("dump_continuation_max_dump_age_minutes", 10.0))
                * 60_000
            )
            fresh_context = (
                int(snapshot.timestamp_ms) - int(state.last_dump_candle_ms) <= max_age_ms
            )
        current_bearish_impulse = True
        if bool(self.settings.get("dump_continuation_require_current_impulse", False)):
            current_bearish_impulse = float(snapshot.price_change_5m_pct) <= -float(
                self.settings.get("dump_continuation_min_current_price_5m_pct", 0.25)
            )
        return {
            "failed_rebound_seen": state.rebound_seen,
            "fresh_after_rebound": int(snapshot.candle_open_time_ms) > state.rebound_candle_ms,
            "fresh_dump_context": fresh_context,
            "current_bearish_impulse": current_bearish_impulse,
            "rejection_structure": rejection_structure,
            "seller_taker": seller_taker,
            "flow_score": flow_votes >= int(
                self.settings.get("dump_continuation_min_flow_votes", 2)
            ),
            "live_entry_not_chased": extension_pct <= max_extension,
            "spread": snapshot.spread_pct <= float(self.settings.get("max_spread_pct", 0.08)),
        }

    def _long_stop_pct(
        self, state: DumpReclaimState, snapshot: MarketSnapshot,
    ) -> float | None:
        entry = float(snapshot.price)
        structural_low = min(value for value in (state.dump_low, state.pullback_low) if value > 0)
        buffer_pct = max(
            float(self.settings.get("dump_reversal_structure_buffer_pct", 0.10)),
            float(snapshot.atr14_pct)
            * float(self.settings.get("dump_reversal_structure_buffer_atr_fraction", 0.10)),
        )
        raw = (entry - structural_low) / entry * 100.0 + buffer_pct
        minimum = float(self.settings.get("dump_reversal_min_stop_pct", 1.50))
        maximum = float(self.settings.get("dump_reversal_max_stop_pct", 4.00))
        return None if raw > maximum else max(minimum, raw)

    def _short_stop_pct(
        self, state: DumpReclaimState, snapshot: MarketSnapshot,
    ) -> float | None:
        entry = float(snapshot.price)
        structural_high = max(
            value for value in (
                state.rebound_high, float(snapshot.previous_candle_high), float(snapshot.candle_high)
            ) if value > 0
        )
        buffer_pct = max(
            float(self.settings.get("dump_continuation_structure_buffer_pct", 0.10)),
            float(snapshot.atr14_pct)
            * float(self.settings.get("dump_continuation_structure_buffer_atr_fraction", 0.10)),
        )
        raw = (structural_high - entry) / entry * 100.0 + buffer_pct
        minimum = float(self.settings.get("dump_continuation_min_stop_pct", 1.20))
        maximum = float(self.settings.get("dump_continuation_max_stop_pct", 2.50))
        return None if raw > maximum else max(minimum, raw)

    def diagnose(self, snapshot: MarketSnapshot) -> dict[str, Any]:
        state = self.states.get(snapshot.symbol)
        if state:
            continuation = self._continuation_checks(state, snapshot)
            if state.phase == "WAIT_PULLBACK":
                reversal = self._pullback_checks(state, snapshot)
            elif state.phase == "WAIT_SECOND_RECLAIM":
                reversal = self._reversal_checks(state, snapshot)
            else:
                reversal = self._first_reclaim_checks(state, snapshot)
            checks = {**{f"REV:{k}": v for k, v in reversal.items()},
                      **{f"CONT:{k}": v for k, v in continuation.items()}}
            phase = state.phase
            direction = "BOTH"
        else:
            checks = {
                "sharp_5m_dump": self._is_sharp_dump(snapshot),
                "spread": snapshot.spread_pct <= float(self.settings.get("max_spread_pct", 0.08)),
            }
            phase, direction = "SCANNING", "BOTH"
        return {
            "symbol": snapshot.symbol, "strategy": self.name, "direction": direction,
            "phase": phase, "checks": checks, "passed": sum(checks.values()),
            "total": len(checks), "blocked_by": [name for name, passed in checks.items() if not passed],
        }

    def observe(self, snapshot: MarketSnapshot) -> StrategyEvent | None:
        now_ms = int(snapshot.timestamp_ms)
        state = self.states.get(snapshot.symbol)
        if state is not None:
            return self._advance(state, snapshot)
        if self._cooldown_active(snapshot.symbol, now_ms) or not self._is_sharp_dump(snapshot):
            return None
        if snapshot.spread_pct > float(self.settings.get("max_spread_pct", 0.08)):
            return None
        ttl_ms = int(float(self.settings.get("dump_reclaim_ttl_minutes", 45)) * 60_000)
        state = DumpReclaimState(
            symbol=snapshot.symbol, armed_at_ms=now_ms, expires_at_ms=now_ms + ttl_ms,
            last_dump_candle_ms=int(snapshot.candle_open_time_ms),
            last_seen_candle_ms=int(snapshot.candle_open_time_ms),
            dump_low=float(snapshot.candle_low), max_dump_pct=float(snapshot.price_change_5m_pct),
            arm_snapshot=snapshot.to_dict(),
        )
        self.states[snapshot.symbol] = state
        return StrategyEvent(
            "ARMED", snapshot.symbol, "BOTH", now_ms, "loose_dump_event_detected",
            {"state": asdict(state), "_strategy": self.name},
        )

    def arm_reentry(
        self, signal: dict[str, Any], result: dict[str, Any], snapshot: MarketSnapshot | None,
    ) -> StrategyEvent | None:
        """Allow one new campaign only when the stop itself forms a fresh sharp dump."""
        evidence = signal.get("evidence") or {}
        if not bool(self.settings.get("dump_reclaim_reentry_enabled", True)):
            return None
        if str(signal.get("setup") or "") not in {self.reversal_setup, self.continuation_setup}:
            return None
        if int(evidence.get("entry_attempt") or 1) >= 2:
            return None
        if str(result.get("exit_reason") or "").upper() != "STOP" or float(result.get("net_pnl") or 0) >= 0:
            return None
        if snapshot is None or not self._is_sharp_dump(snapshot):
            return None
        now_ms = int(result.get("closed_at_ms") or snapshot.timestamp_ms)
        ttl_ms = int(float(self.settings.get("dump_reclaim_reentry_ttl_minutes", 40)) * 60_000)
        state = DumpReclaimState(
            symbol=str(signal["symbol"]), armed_at_ms=now_ms, expires_at_ms=now_ms + ttl_ms,
            last_dump_candle_ms=int(snapshot.candle_open_time_ms),
            last_seen_candle_ms=int(snapshot.candle_open_time_ms),
            dump_low=float(snapshot.candle_low), max_dump_pct=float(snapshot.price_change_5m_pct),
            arm_snapshot={
                "fresh_dump_after_stop": True, "prior_signal": signal,
                "stop_result": result, "market": snapshot.to_dict(),
            },
            attempt=2, source_execution_id=int(result.get("execution_id") or 0) or None,
        )
        self.states[state.symbol] = state
        self.last_signal_ms.pop(state.symbol, None)
        return StrategyEvent(
            "ARMED", state.symbol, "BOTH", now_ms, "fresh_dump_after_stop",
            {"state": asdict(state), "_strategy": self.name},
        )

    def _signal(
        self, state: DumpReclaimState, snapshot: MarketSnapshot, direction: str,
        setup: str, stop_pct: float, target_pct: float, checks: dict[str, bool], reason: str,
    ) -> StrategyEvent:
        entry = float(snapshot.price)
        long = direction == "LONG"
        stop = entry * (1.0 - stop_pct / 100.0) if long else entry * (1.0 + stop_pct / 100.0)
        target = entry * (1.0 + target_pct / 100.0) if long else entry * (1.0 - target_pct / 100.0)
        rebound_from_low_pct = self._change_pct(entry, state.dump_low)
        signal = TradeSignal(
            symbol=state.symbol, direction=direction, timestamp_ms=int(snapshot.timestamp_ms),
            entry_price=entry, stop_price=stop, target_price=target, risk_pct=stop_pct,
            setup=setup,
            evidence={
                "arm": state.arm_snapshot, "entry": snapshot.to_dict(),
                "confirmation_checks": checks, "classification": setup,
                "dump_low": state.dump_low, "max_dump_pct": state.max_dump_pct,
                "rebound_from_dump_low_pct": rebound_from_low_pct,
                "rebound_high": state.rebound_high, "entry_attempt": state.attempt,
                "reentry_after_stop": state.attempt == 2,
                "source_execution_id": state.source_execution_id,
                "first_reclaim_candle_ms": state.first_reclaim_candle_ms,
                "pullback_candle_ms": state.pullback_candle_ms,
                "pullback_low": state.pullback_low,
                "strategy_version": str(
                    self.settings.get("dump_reclaim_strategy_version", "DUAL_CLASSIFIER_V3")
                ),
                "research_model": "LOOSE_DUMP_DETECTOR_DUAL_DIRECTION_CLASSIFIER",
            },
        )
        self.states.pop(state.symbol, None)
        self.last_signal_ms[state.symbol] = int(snapshot.timestamp_ms)
        return StrategyEvent(
            "SIGNAL", state.symbol, direction, int(snapshot.timestamp_ms), reason, signal.to_dict(),
        )

    def _advance(self, state: DumpReclaimState, snapshot: MarketSnapshot) -> StrategyEvent | None:
        now_ms, candle_ms = int(snapshot.timestamp_ms), int(snapshot.candle_open_time_ms)
        if now_ms >= state.expires_at_ms:
            self.states.pop(state.symbol, None)
            return StrategyEvent("EXPIRED", state.symbol, "BOTH", now_ms, "dump_classifier_ttl", {})
        if candle_ms <= state.last_seen_candle_ms:
            return None
        state.last_seen_candle_ms = candle_ms
        prior_dump_low = state.dump_low

        continuation = self._continuation_checks(state, snapshot, prior_dump_low)
        if all(continuation.values()):
            stop_pct = self._short_stop_pct(state, snapshot)
            if stop_pct is not None:
                target_pct = stop_pct * float(
                    self.settings.get("dump_continuation_target_r", 2.0)
                )
                return self._signal(
                    state, snapshot, "SHORT", self.continuation_setup, stop_pct, target_pct,
                    continuation, "failed_rebound_dump_continuation",
                )

        if self._is_sharp_dump(snapshot):
            state.dump_low = min(state.dump_low, float(snapshot.candle_low))
            state.last_dump_candle_ms = candle_ms
            state.max_dump_pct = min(state.max_dump_pct, float(snapshot.price_change_5m_pct))
            state.phase = "WAIT_FIRST_RECLAIM"
            state.first_reclaim_candle_ms = 0
            state.first_reclaim_high = 0.0
            state.pullback_candle_ms = 0
            state.pullback_low = 0.0
            ttl_ms = int(float(self.settings.get("dump_reclaim_ttl_minutes", 45)) * 60_000)
            state.expires_at_ms = now_ms + ttl_ms
            return StrategyEvent(
                "EXTENDED", state.symbol, "BOTH", now_ms, "additional_sharp_dump",
                {"state": asdict(state), "continuation_checks": continuation},
            )

        self._record_rebound(state, snapshot)
        if state.phase == "WAIT_FIRST_RECLAIM":
            checks = self._first_reclaim_checks(state, snapshot)
            if not all(checks.values()):
                return None
            state.phase = "WAIT_PULLBACK"
            state.first_reclaim_candle_ms = candle_ms
            state.first_reclaim_high = float(snapshot.candle_high)
            return StrategyEvent(
                "RECLAIM", state.symbol, "LONG", now_ms, "first_reclaim_waiting_pullback",
                {"state": asdict(state), "checks": checks},
            )
        if state.phase == "WAIT_PULLBACK":
            checks = self._pullback_checks(state, snapshot)
            if not all(checks.values()):
                return None
            state.phase = "WAIT_SECOND_RECLAIM"
            state.pullback_candle_ms = candle_ms
            state.pullback_low = float(snapshot.candle_low)
            return StrategyEvent(
                "PULLBACK", state.symbol, "LONG", now_ms, "pullback_preserved_dump_floor",
                {"state": asdict(state), "checks": checks},
            )

        checks = self._reversal_checks(state, snapshot)
        if not all(checks.values()):
            return None
        stop_pct = self._long_stop_pct(state, snapshot)
        if stop_pct is None:
            return StrategyEvent(
                "BLOCKED", state.symbol, "LONG", now_ms, "reversal_structure_stop_too_wide",
                {"state": asdict(state), "checks": checks},
            )
        return self._signal(
            state, snapshot, "LONG", self.reversal_setup, stop_pct,
            max(
                float(self.settings.get("dump_reversal_target_pct", 3.0)),
                stop_pct * float(self.settings.get("dump_reversal_min_target_r", 1.75)),
            ), checks,
            "confirmed_dump_exhaustion_reversal",
        )

    def snapshot(self) -> dict[str, Any]:
        return {symbol: asdict(state) for symbol, state in self.states.items()}
