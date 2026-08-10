from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from engine.binance import MarketSnapshot
from engine.strategy import StrategyEvent, TradeSignal


def _now(snapshot: MarketSnapshot) -> int:
    return int(snapshot.timestamp_ms or time.time() * 1000)


def _change_pct(current: float, previous: float) -> float:
    return (current - previous) / previous * 100.0 if previous else 0.0


@dataclass
class LongCampaignState:
    symbol: str
    armed_at_ms: int
    expires_at_ms: int
    boundary: float
    structural_low: float
    arm_oi_change_15m_pct: float
    arm_lsr: float
    arm_candle_open_time_ms: int
    phase: str = "ACCUMULATION_ARMED"
    breakout_at_ms: int = 0
    breakout_candle_open_time_ms: int = 0
    breakout_price: float = 0.0
    arm_snapshot: dict[str, Any] = field(default_factory=dict)


class FlowLongCampaignStrategy:
    """Long-only accumulation campaign confirmed by structure, OI and LSR persistence."""

    name = "FLOW_LONG_CAMPAIGN"

    def __init__(self, settings: dict[str, Any]):
        self.settings = settings
        self.states: dict[str, LongCampaignState] = {}
        self.last_signal_ms: dict[str, int] = {}

    def _cooldown(self, symbol: str, now_ms: int) -> bool:
        last = self.last_signal_ms.get(symbol)
        cooldown = int(float(self.settings.get("long_campaign_cooldown_minutes", 30)) * 60_000)
        return last is not None and now_ms - last < cooldown

    def _arm_checks(self, snapshot: MarketSnapshot) -> dict[str, bool]:
        cfg = self.settings
        distance = snapshot.distance_to_prior_high_pct
        return {
            "compression": snapshot.compression_ratio <= float(cfg.get("long_campaign_max_compression_ratio", 0.95)),
            "range_compact": snapshot.range_width_pct <= float(cfg.get("long_campaign_max_range_width_pct", 1.80)),
            "near_boundary": (
                -float(cfg.get("long_campaign_max_prearm_extension_pct", 0.05))
                <= distance
                <= float(cfg.get("long_campaign_near_boundary_pct", 0.40))
            ),
            "oi_buildup": snapshot.oi_change_15m_pct >= float(cfg.get("long_campaign_min_arm_oi_15m_pct", 0.05)),
            "lsr_bias": snapshot.lsr_change_5m_pct <= float(cfg.get("long_campaign_max_arm_lsr_slope_pct", 0.10)),
            "volume_alive": snapshot.volume_ratio >= float(cfg.get("long_campaign_min_arm_volume_ratio", 0.65)),
            "taker_not_climax": snapshot.taker_buy_sell_ratio <= float(cfg.get("long_campaign_max_arm_taker", 1.80)),
            "funding": snapshot.funding_rate_pct <= float(cfg.get("long_campaign_max_funding_pct", 0.05)),
            "spread": snapshot.spread_pct <= float(cfg.get("max_spread_pct", 0.08)),
        }

    def _breakout_checks(self, state: LongCampaignState, snapshot: MarketSnapshot) -> dict[str, bool]:
        cfg = self.settings
        buffer = float(cfg.get("long_campaign_breakout_buffer_pct", 0.04)) / 100.0
        return {
            "fresh_closed_candle": snapshot.candle_open_time_ms > state.arm_candle_open_time_ms,
            "breakout_close": snapshot.candle_close > state.boundary * (1.0 + buffer),
            "price_holds": snapshot.price > state.boundary,
            "oi_persists": snapshot.oi_change_15m_pct >= float(cfg.get("long_campaign_min_breakout_oi_15m_pct", 0.10)),
            "oi_not_decelerating": snapshot.oi_acceleration_5m_pct_points >= float(cfg.get("long_campaign_min_oi_acceleration_points", -0.05)),
            "lsr_falls": snapshot.lsr_change_5m_pct <= float(cfg.get("long_campaign_max_breakout_lsr_slope_pct", 0.0)),
            "taker_confirms": snapshot.taker_buy_sell_ratio >= float(cfg.get("long_campaign_min_breakout_taker", 1.02)),
            "volume_confirms": snapshot.volume_ratio >= float(cfg.get("long_campaign_min_breakout_volume_ratio", 1.0)),
            "spread": snapshot.spread_pct <= float(cfg.get("max_spread_pct", 0.08)),
        }

    def _confirmation_checks(self, state: LongCampaignState, snapshot: MarketSnapshot) -> dict[str, bool]:
        cfg = self.settings
        retest_tolerance = float(cfg.get("long_campaign_retest_tolerance_pct", 0.20)) / 100.0
        hold_buffer = float(cfg.get("long_campaign_hold_buffer_pct", 0.02)) / 100.0
        sustained = (
            snapshot.previous_candle_close > state.boundary
            and snapshot.candle_close > state.boundary * (1.0 + hold_buffer)
        )
        retest = (
            snapshot.candle_low <= state.boundary * (1.0 + retest_tolerance)
            and snapshot.candle_close > state.boundary
            and snapshot.candle_close > snapshot.candle_open
        )
        extension = _change_pct(snapshot.price, state.boundary)
        return {
            "fresh_confirmation_candle": snapshot.candle_open_time_ms > state.breakout_candle_open_time_ms,
            "sustain_or_retest": sustained or retest,
            "oi_persists": snapshot.oi_change_15m_pct >= float(cfg.get("long_campaign_min_confirm_oi_15m_pct", 0.08)),
            "oi_not_contracting": snapshot.oi_change_5m_pct >= float(cfg.get("long_campaign_min_confirm_oi_5m_pct", -0.08)),
            "lsr_below_arm": snapshot.global_lsr <= state.arm_lsr,
            "lsr_not_reversing": snapshot.lsr_change_5m_pct <= float(cfg.get("long_campaign_max_confirm_lsr_slope_pct", 0.15)),
            "taker_supports": snapshot.taker_buy_sell_ratio >= float(cfg.get("long_campaign_min_confirm_taker", 1.0)),
            "volume_alive": snapshot.volume_ratio >= float(cfg.get("long_campaign_min_confirm_volume_ratio", 0.80)),
            "not_extended": extension <= float(cfg.get("long_campaign_max_entry_extension_pct", 0.65)),
            "spread": snapshot.spread_pct <= float(cfg.get("max_spread_pct", 0.08)),
        }

    def diagnose(self, snapshot: MarketSnapshot) -> dict[str, Any]:
        state = self.states.get(snapshot.symbol)
        if state is None:
            checks = self._arm_checks(snapshot)
            phase = "SCANNING"
        elif state.phase == "BREAKOUT_DETECTED":
            checks = self._confirmation_checks(state, snapshot)
            phase = state.phase
        else:
            checks = self._breakout_checks(state, snapshot)
            phase = state.phase
        return {
            "symbol": snapshot.symbol,
            "strategy": self.name,
            "direction": "LONG",
            "phase": phase,
            "checks": checks,
            "passed": sum(checks.values()),
            "total": len(checks),
            "blocked_by": [key for key, value in checks.items() if not value],
        }

    def observe(self, snapshot: MarketSnapshot) -> StrategyEvent | None:
        now_ms = _now(snapshot)
        state = self.states.get(snapshot.symbol)
        if state is not None:
            return self._advance(state, snapshot)
        if self._cooldown(snapshot.symbol, now_ms):
            return None
        checks = self._arm_checks(snapshot)
        if not all(checks.values()):
            return None
        ttl_ms = int(float(self.settings.get("long_campaign_setup_ttl_minutes", 60)) * 60_000)
        state = LongCampaignState(
            symbol=snapshot.symbol,
            armed_at_ms=now_ms,
            expires_at_ms=now_ms + ttl_ms,
            boundary=snapshot.prior_high,
            structural_low=min(snapshot.prior_low, snapshot.candle_low),
            arm_oi_change_15m_pct=snapshot.oi_change_15m_pct,
            arm_lsr=snapshot.global_lsr,
            arm_candle_open_time_ms=snapshot.candle_open_time_ms,
            arm_snapshot=snapshot.to_dict(),
        )
        self.states[snapshot.symbol] = state
        return StrategyEvent(
            "CAMPAIGN_ARMED", snapshot.symbol, "LONG", now_ms, "accumulation_oi_lsr_aligned",
            {"checks": checks, "state": asdict(state)},
        )

    def _advance(self, state: LongCampaignState, snapshot: MarketSnapshot) -> StrategyEvent | None:
        now_ms = _now(snapshot)
        if now_ms >= state.expires_at_ms:
            del self.states[state.symbol]
            return StrategyEvent("EXPIRED", state.symbol, "LONG", now_ms, "long_campaign_ttl", {})
        invalidation_buffer = float(self.settings.get("long_campaign_invalidation_buffer_pct", 0.10)) / 100.0
        if snapshot.candle_close < state.structural_low * (1.0 - invalidation_buffer):
            del self.states[state.symbol]
            return StrategyEvent("INVALIDATED", state.symbol, "LONG", now_ms, "accumulation_structure_lost", {})

        if state.phase == "ACCUMULATION_ARMED":
            checks = self._breakout_checks(state, snapshot)
            if not all(checks.values()):
                return None
            state.phase = "BREAKOUT_DETECTED"
            state.breakout_at_ms = now_ms
            state.breakout_candle_open_time_ms = snapshot.candle_open_time_ms
            state.breakout_price = snapshot.candle_close
            return StrategyEvent(
                "BREAKOUT_DETECTED", state.symbol, "LONG", now_ms, "long_breakout_waiting_hold_or_retest",
                {"checks": checks, "state": asdict(state)},
            )

        checks = self._confirmation_checks(state, snapshot)
        if not all(checks.values()):
            return None
        entry = snapshot.price
        stop_buffer = float(self.settings.get("long_campaign_stop_below_boundary_pct", 0.35)) / 100.0
        stop = state.boundary * (1.0 - stop_buffer)
        risk_pct = abs(entry - stop) / entry * 100.0 if entry else 0.0
        if risk_pct <= 0 or risk_pct > float(self.settings.get("long_campaign_max_stop_pct", 1.40)):
            del self.states[state.symbol]
            return StrategyEvent("INVALIDATED", state.symbol, "LONG", now_ms, "long_campaign_stop_too_wide", {"risk_pct": risk_pct})
        risk = entry - stop
        target_r = float(self.settings.get("long_campaign_target_r", 2.50))
        signal = TradeSignal(
            symbol=state.symbol,
            direction="LONG",
            timestamp_ms=now_ms,
            entry_price=entry,
            stop_price=stop,
            target_price=entry + risk * target_r,
            risk_pct=risk_pct,
            setup=self.name,
            evidence={
                "arm": state.arm_snapshot,
                "entry": snapshot.to_dict(),
                "confirmation_checks": checks,
                "boundary": state.boundary,
                "breakout_price": state.breakout_price,
                "oi_change_from_arm_pct_points": snapshot.oi_change_15m_pct - state.arm_oi_change_15m_pct,
                "lsr_change_from_arm_pct": _change_pct(snapshot.global_lsr, state.arm_lsr),
            },
        )
        del self.states[state.symbol]
        self.last_signal_ms[state.symbol] = now_ms
        return StrategyEvent("SIGNAL", state.symbol, "LONG", now_ms, "long_campaign_confirmed", signal.to_dict())

    def snapshot(self) -> dict[str, Any]:
        return {symbol: asdict(state) for symbol, state in self.states.items()}


@dataclass
class StructuralReversalState:
    symbol: str
    direction: str
    armed_at_ms: int
    expires_at_ms: int
    extreme_price: float
    arm_oi_change_15m_pct: float
    arm_lsr: float
    arm_candle_open_time_ms: int
    phase: str = "EXTREME_ARMED"
    choch_level: float = 0.0
    choch_at_ms: int = 0
    choch_candle_open_time_ms: int = 0
    arm_snapshot: dict[str, Any] = field(default_factory=dict)


class FlowStructuralReversalStrategy:
    """Reverse an extreme move only after CHoCH, OI deterioration and a persistent hold."""

    name = "FLOW_STRUCTURAL_REVERSAL"

    def __init__(self, settings: dict[str, Any]):
        self.settings = settings
        self.states: dict[str, StructuralReversalState] = {}
        self.last_signal_ms: dict[str, int] = {}

    def _direction(self, snapshot: MarketSnapshot) -> str:
        return "SHORT" if snapshot.price_change_15m_pct > 0 else "LONG"

    def _cooldown(self, symbol: str, now_ms: int) -> bool:
        last = self.last_signal_ms.get(symbol)
        cooldown = int(float(self.settings.get("structural_reversal_cooldown_minutes", 30)) * 60_000)
        return last is not None and now_ms - last < cooldown

    def _arm_checks(self, snapshot: MarketSnapshot, direction: str) -> dict[str, bool]:
        cfg = self.settings
        short = direction == "SHORT"
        impulse = snapshot.price_change_15m_pct
        breakout = snapshot.price > snapshot.prior_high if short else snapshot.price < snapshot.prior_low
        taker_extreme = (
            snapshot.taker_buy_sell_ratio >= float(cfg.get("structural_reversal_up_taker_climax", 1.55))
            if short else snapshot.taker_buy_sell_ratio <= float(cfg.get("structural_reversal_down_taker_climax", 0.68))
        )
        lsr_extreme = (
            snapshot.global_lsr >= float(cfg.get("structural_reversal_up_min_lsr", 1.45))
            if short else snapshot.global_lsr <= float(cfg.get("structural_reversal_down_max_lsr", 0.80))
        )
        threshold = float(cfg.get("structural_reversal_min_price_impulse_pct", 1.0))
        return {
            "extreme_impulse": impulse >= threshold if short else impulse <= -threshold,
            "outside_structure": breakout,
            "oi_expansion": snapshot.oi_change_15m_pct >= float(cfg.get("structural_reversal_min_arm_oi_15m_pct", 0.20)),
            "volume_climax": snapshot.volume_ratio >= float(cfg.get("structural_reversal_min_arm_volume_ratio", 1.40)),
            "positioning_extreme": taker_extreme or lsr_extreme,
            "spread": snapshot.spread_pct <= float(cfg.get("max_spread_pct", 0.08)),
        }

    def _choch_checks(self, state: StructuralReversalState, snapshot: MarketSnapshot) -> dict[str, bool]:
        cfg = self.settings
        short = state.direction == "SHORT"
        sign = -1.0 if short else 1.0
        close = snapshot.candle_close or snapshot.price
        reversal = (close - state.extreme_price) / state.extreme_price * 100.0 * sign
        structure_break = (
            close < snapshot.previous_candle_low and close < snapshot.candle_open
            if short else close > snapshot.previous_candle_high and close > snapshot.candle_open
        )
        taker_flip = (
            snapshot.taker_buy_sell_ratio <= float(cfg.get("structural_reversal_short_confirm_taker", 0.90))
            if short else snapshot.taker_buy_sell_ratio >= float(cfg.get("structural_reversal_long_confirm_taker", 1.10))
        )
        oi_drop = state.arm_oi_change_15m_pct - snapshot.oi_change_15m_pct
        lsr_unwinds = (
            snapshot.global_lsr < state.arm_lsr and snapshot.lsr_change_5m_pct <= 0.0
            if short else snapshot.global_lsr > state.arm_lsr and snapshot.lsr_change_5m_pct >= 0.0
        )
        return {
            "fresh_window": _now(snapshot) - state.armed_at_ms >= int(float(cfg.get("structural_reversal_min_age_minutes", 5)) * 60_000),
            "fresh_closed_candle": snapshot.candle_open_time_ms > state.arm_candle_open_time_ms,
            "price_reversal": reversal >= float(cfg.get("structural_reversal_min_retracement_pct", 0.30)),
            "choch": structure_break,
            "oi_15m_deteriorates": oi_drop >= float(cfg.get("structural_reversal_min_oi_deterioration_points", 0.15)),
            "oi_5m_not_expanding": snapshot.oi_change_5m_pct <= float(cfg.get("structural_reversal_max_oi_5m_pct", 0.0)),
            "lsr_unwinds": lsr_unwinds,
            "taker_flips": taker_flip,
            "volume_alive": snapshot.volume_ratio >= float(cfg.get("structural_reversal_min_confirm_volume_ratio", 0.80)),
            "spread": snapshot.spread_pct <= float(cfg.get("max_spread_pct", 0.08)),
        }

    def _hold_checks(self, state: StructuralReversalState, snapshot: MarketSnapshot) -> dict[str, bool]:
        cfg = self.settings
        short = state.direction == "SHORT"
        close = snapshot.candle_close or snapshot.price
        structure_holds = close < state.choch_level if short else close > state.choch_level
        taker_holds = (
            snapshot.taker_buy_sell_ratio <= float(cfg.get("structural_reversal_short_hold_taker", 1.0))
            if short else snapshot.taker_buy_sell_ratio >= float(cfg.get("structural_reversal_long_hold_taker", 1.0))
        )
        oi_drop = state.arm_oi_change_15m_pct - snapshot.oi_change_15m_pct
        return {
            "fresh_hold_candle": snapshot.candle_open_time_ms > state.choch_candle_open_time_ms,
            "structure_holds": structure_holds,
            "oi_deterioration_persists": oi_drop >= float(cfg.get("structural_reversal_min_oi_deterioration_points", 0.15)),
            "oi_5m_not_expanding": snapshot.oi_change_5m_pct <= float(cfg.get("structural_reversal_max_hold_oi_5m_pct", 0.05)),
            "taker_holds": taker_holds,
            "volume_alive": snapshot.volume_ratio >= float(cfg.get("structural_reversal_min_hold_volume_ratio", 0.60)),
            "spread": snapshot.spread_pct <= float(cfg.get("max_spread_pct", 0.08)),
        }

    def diagnose(self, snapshot: MarketSnapshot) -> dict[str, Any]:
        state = self.states.get(snapshot.symbol)
        direction = state.direction if state else self._direction(snapshot)
        if state is None:
            checks = self._arm_checks(snapshot, direction)
            phase = "SCANNING"
        elif state.phase == "CHOCH_DETECTED":
            checks = self._hold_checks(state, snapshot)
            phase = state.phase
        else:
            checks = self._choch_checks(state, snapshot)
            phase = state.phase
        return {
            "symbol": snapshot.symbol,
            "strategy": self.name,
            "direction": direction,
            "phase": phase,
            "checks": checks,
            "passed": sum(checks.values()),
            "total": len(checks),
            "blocked_by": [key for key, value in checks.items() if not value],
        }

    def observe(self, snapshot: MarketSnapshot) -> StrategyEvent | None:
        now_ms = _now(snapshot)
        state = self.states.get(snapshot.symbol)
        if state is not None:
            return self._advance(state, snapshot)
        if self._cooldown(snapshot.symbol, now_ms):
            return None
        direction = self._direction(snapshot)
        checks = self._arm_checks(snapshot, direction)
        if not all(checks.values()):
            return None
        ttl_ms = int(float(self.settings.get("structural_reversal_setup_ttl_minutes", 45)) * 60_000)
        state = StructuralReversalState(
            symbol=snapshot.symbol,
            direction=direction,
            armed_at_ms=now_ms,
            expires_at_ms=now_ms + ttl_ms,
            extreme_price=snapshot.candle_high if direction == "SHORT" else snapshot.candle_low,
            arm_oi_change_15m_pct=snapshot.oi_change_15m_pct,
            arm_lsr=snapshot.global_lsr,
            arm_candle_open_time_ms=snapshot.candle_open_time_ms,
            arm_snapshot=snapshot.to_dict(),
        )
        self.states[snapshot.symbol] = state
        return StrategyEvent(
            "EXTREME_ARMED", snapshot.symbol, direction, now_ms, "crowded_extreme_waiting_choch",
            {"checks": checks, "state": asdict(state)},
        )

    def _advance(self, state: StructuralReversalState, snapshot: MarketSnapshot) -> StrategyEvent | None:
        now_ms = _now(snapshot)
        if now_ms >= state.expires_at_ms:
            del self.states[state.symbol]
            return StrategyEvent("EXPIRED", state.symbol, state.direction, now_ms, "structural_reversal_ttl", {})
        short = state.direction == "SHORT"
        if state.phase == "EXTREME_ARMED":
            new_extreme = snapshot.candle_high > state.extreme_price if short else snapshot.candle_low < state.extreme_price
            if new_extreme:
                state.extreme_price = snapshot.candle_high if short else snapshot.candle_low
            checks = self._choch_checks(state, snapshot)
            if not all(checks.values()):
                return None
            state.phase = "CHOCH_DETECTED"
            state.choch_at_ms = now_ms
            state.choch_candle_open_time_ms = snapshot.candle_open_time_ms
            state.choch_level = snapshot.previous_candle_low if short else snapshot.previous_candle_high
            return StrategyEvent(
                "CHOCH_DETECTED", state.symbol, state.direction, now_ms, "structure_and_flow_reversed_waiting_hold",
                {"checks": checks, "state": asdict(state)},
            )

        checks = self._hold_checks(state, snapshot)
        if not all(checks.values()):
            return None
        entry = snapshot.price
        stop_buffer = float(self.settings.get("structural_reversal_stop_buffer_pct", 0.15)) / 100.0
        stop = state.extreme_price * (1.0 + stop_buffer if short else 1.0 - stop_buffer)
        risk_pct = abs(entry - stop) / entry * 100.0 if entry else 0.0
        if risk_pct <= 0 or risk_pct > float(self.settings.get("structural_reversal_max_stop_pct", 1.50)):
            del self.states[state.symbol]
            return StrategyEvent("INVALIDATED", state.symbol, state.direction, now_ms, "structural_reversal_stop_too_wide", {"risk_pct": risk_pct})
        risk = abs(entry - stop)
        target_r = float(self.settings.get("structural_reversal_target_r", 2.50))
        target = entry - risk * target_r if short else entry + risk * target_r
        signal = TradeSignal(
            symbol=state.symbol,
            direction=state.direction,
            timestamp_ms=now_ms,
            entry_price=entry,
            stop_price=stop,
            target_price=target,
            risk_pct=risk_pct,
            setup=self.name,
            evidence={
                "arm": state.arm_snapshot,
                "entry": snapshot.to_dict(),
                "confirmation_checks": checks,
                "choch_level": state.choch_level,
                "squeeze_extreme": state.extreme_price,
                "oi_change_from_arm_pct_points": snapshot.oi_change_15m_pct - state.arm_oi_change_15m_pct,
                "lsr_change_from_arm_pct": _change_pct(snapshot.global_lsr, state.arm_lsr),
            },
        )
        del self.states[state.symbol]
        self.last_signal_ms[state.symbol] = now_ms
        return StrategyEvent("SIGNAL", state.symbol, state.direction, now_ms, "structural_reversal_confirmed", signal.to_dict())

    def snapshot(self) -> dict[str, Any]:
        return {symbol: asdict(state) for symbol, state in self.states.items()}


# Compatibility aliases for imports outside the operational service.
FlowBreakoutContinuationStrategy = FlowLongCampaignStrategy
FlowExhaustionReversalStrategy = FlowStructuralReversalStrategy
