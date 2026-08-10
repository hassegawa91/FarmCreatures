from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from engine.binance import MarketSnapshot


@dataclass
class SetupState:
    symbol: str
    direction: str
    armed_at_ms: int
    expires_at_ms: int
    breakout_price: float
    favorable_extreme: float
    pullback_extreme: float | None = None
    pullback_candle_ms: int = 0
    phase: str = "ARMED"
    arm_snapshot: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TradeSignal:
    symbol: str
    direction: str
    timestamp_ms: int
    entry_price: float
    stop_price: float
    target_price: float
    risk_pct: float
    setup: str
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StrategyEvent:
    type: str
    symbol: str
    direction: str
    timestamp_ms: int
    reason: str
    payload: dict[str, Any]


class OIBreakoutRetestStrategy:
    """OI expansion: buildup, a fresh confirmation window, then immediate continuation."""

    def __init__(self, settings: dict[str, Any]):
        self.settings = settings
        self.states: dict[str, SetupState] = {}
        self.last_signal_ms: dict[str, int] = {}

    def _initial_checks(self, snapshot: MarketSnapshot, direction: str) -> dict[str, bool]:
        cfg = self.settings
        long = direction == "LONG"
        return {
            "price_impulse": snapshot.price_change_15m_pct >= float(cfg["min_price_impulse_pct"])
            if long else snapshot.price_change_15m_pct <= -float(cfg["min_price_impulse_pct"]),
            "breakout": snapshot.price > snapshot.prior_high if long else snapshot.price < snapshot.prior_low,
            "oi_expansion": snapshot.oi_change_15m_pct >= float(cfg["min_oi_change_15m_pct"]),
            "volume": snapshot.volume_ratio >= float(cfg["min_volume_ratio"]),
            "taker": snapshot.taker_buy_sell_ratio >= float(cfg["long_min_taker_ratio"])
            if long else snapshot.taker_buy_sell_ratio <= float(cfg["short_max_taker_ratio"]),
            "taker_not_climax": snapshot.taker_buy_sell_ratio <= float(cfg.get("long_max_taker_ratio", 999.0))
            if long else snapshot.taker_buy_sell_ratio >= float(cfg.get("short_min_taker_ratio", 0.0)),
            "crowding": snapshot.global_lsr <= float(cfg["long_max_global_lsr"])
            if long else snapshot.global_lsr >= float(cfg["short_min_global_lsr"]),
            "funding": snapshot.funding_rate_pct <= float(cfg["long_max_funding_pct"])
            if long else snapshot.funding_rate_pct >= float(cfg["short_min_funding_pct"]),
            "spread": snapshot.spread_pct <= float(cfg["max_spread_pct"]),
        }

    def _confirmation_checks(self, state: SetupState, snapshot: MarketSnapshot) -> dict[str, bool]:
        cfg = self.settings
        long = state.direction == "LONG"
        arm = state.arm_snapshot
        sign = 1.0 if long else -1.0
        extension = (snapshot.price - state.breakout_price) / state.breakout_price * 100.0 * sign
        oi_acceleration = snapshot.oi_change_15m_pct - float(arm["oi_change_15m_pct"])
        arm_oi_timestamp = int(arm.get("oi_timestamp_ms") or 0)
        elapsed_ms = snapshot.timestamp_ms - state.armed_at_ms
        direct_window_ms = int(float(cfg.get("confirmation_min_minutes", 5)) * 60_000)
        is_retest = state.phase == "PULLBACK" and state.pullback_extreme is not None
        fresh_retest_candle = (
            snapshot.micro_open_time_ms > state.pullback_candle_ms
            if snapshot.micro_open_time_ms and state.pullback_candle_ms else
            elapsed_ms >= int(float(cfg.get("retest_min_minutes", 1)) * 60_000)
        )
        fresh_oi_sample = snapshot.oi_timestamp_ms > arm_oi_timestamp if arm_oi_timestamp else (
            elapsed_ms >= direct_window_ms
        )
        oi_age_ms = max(0, snapshot.timestamp_ms - snapshot.oi_timestamp_ms) if snapshot.oi_timestamp_ms else direct_window_ms + 1
        held_oi_sample_allowed = bool(cfg.get("retest_allow_held_oi_sample", True)) and (
            oi_age_ms <= int(float(cfg.get("retest_max_oi_sample_age_minutes", 6)) * 60_000)
        )
        oi_holds = snapshot.oi_change_15m_pct >= max(
            float(cfg["min_oi_change_15m_pct"]),
            float(arm["oi_change_15m_pct"]) - float(cfg.get("oi_hold_tolerance_pct_points", 0.08)),
        )
        lsr_limit = float(cfg.get("max_lsr_deterioration_pct", 3.0)) / 100.0
        lsr_healthy = (
            snapshot.global_lsr <= float(arm["global_lsr"]) * (1.0 + lsr_limit)
            if long else snapshot.global_lsr >= float(arm["global_lsr"]) * (1.0 - lsr_limit)
        )
        taker_healthy = (
            float(cfg["long_min_taker_ratio"]) <= snapshot.taker_buy_sell_ratio <= float(cfg["long_max_taker_ratio"])
            if long else float(cfg["short_min_taker_ratio"]) <= snapshot.taker_buy_sell_ratio <= float(cfg["short_max_taker_ratio"])
        )
        boundary = float(arm["prior_high"] if long else arm["prior_low"])
        boundary_extension = (snapshot.price - boundary) / boundary * 100.0 * sign
        if is_retest:
            resume_extension = (
                (snapshot.price - state.pullback_extreme) / state.pullback_extreme * 100.0 * sign
            )
            price_confirmation = resume_extension >= float(cfg.get("retest_reclaim_pct", 0.08))
        else:
            price_confirmation = extension >= float(cfg.get("min_price_extension_pct", 0.12))
        return {
            "fresh_window": fresh_retest_candle if is_retest else elapsed_ms >= direct_window_ms,
            "fresh_oi": (fresh_oi_sample or held_oi_sample_allowed) if is_retest else fresh_oi_sample,
            "price_confirmation": price_confirmation,
            "oi_holds": oi_holds,
            "breakout_hold": snapshot.price > boundary if long else snapshot.price < boundary,
            "not_overextended": boundary_extension <= float(cfg.get("max_entry_extension_from_boundary_pct", 0.55)),
            "volume": snapshot.volume_ratio >= float(cfg.get("min_entry_volume_ratio", 1.0)),
            "taker_not_climax": taker_healthy,
            "lsr_not_deteriorating": lsr_healthy,
            "funding": snapshot.funding_rate_pct <= float(cfg["long_max_funding_pct"])
            if long else snapshot.funding_rate_pct >= float(cfg["short_min_funding_pct"]),
            "spread": snapshot.spread_pct <= float(cfg["max_spread_pct"]),
        }

    def diagnose(self, snapshot: MarketSnapshot) -> dict[str, Any]:
        state = self.states.get(snapshot.symbol)
        direction = state.direction if state else ("LONG" if snapshot.price_change_15m_pct > 0 else "SHORT")
        checks = self._initial_checks(snapshot, direction)
        return {
            "symbol": snapshot.symbol,
            "strategy": str(self.settings.get("name") or "OI_EXPANSION_CONFIRMATION"),
            "direction": direction,
            "phase": state.phase if state else "BLOCKED",
            "checks": checks,
            "passed": sum(checks.values()),
            "total": len(checks),
            "blocked_by": [name for name, passed in checks.items() if not passed],
        }

    def _cooldown_active(self, symbol: str, now_ms: int) -> bool:
        cooldown_ms = int(float(self.settings.get("cooldown_minutes", 60)) * 60_000)
        last = self.last_signal_ms.get(symbol)
        return last is not None and now_ms - last < cooldown_ms

    def observe(self, snapshot: MarketSnapshot) -> StrategyEvent | None:
        now_ms = snapshot.timestamp_ms or int(time.time() * 1000)
        state = self.states.get(snapshot.symbol)
        if state:
            return self._advance(state, snapshot)
        if self._cooldown_active(snapshot.symbol, now_ms):
            return None
        direction = "LONG" if snapshot.price_change_15m_pct > 0 else "SHORT"
        checks = self._initial_checks(snapshot, direction)
        if not all(checks.values()):
            return None
        ttl_ms = int(float(self.settings.get("setup_ttl_minutes", 45)) * 60_000)
        state = SetupState(
            symbol=snapshot.symbol,
            direction=direction,
            armed_at_ms=now_ms,
            expires_at_ms=now_ms + ttl_ms,
            breakout_price=snapshot.price,
            favorable_extreme=snapshot.price,
            arm_snapshot=snapshot.to_dict(),
        )
        self.states[snapshot.symbol] = state
        return StrategyEvent("ARMED", snapshot.symbol, direction, now_ms, "positioning_breakout", {"checks": checks, "state": asdict(state)})

    def _advance(self, state: SetupState, snapshot: MarketSnapshot) -> StrategyEvent | None:
        now_ms = snapshot.timestamp_ms
        long = state.direction == "LONG"
        if now_ms >= state.expires_at_ms:
            del self.states[state.symbol]
            return StrategyEvent("EXPIRED", state.symbol, state.direction, now_ms, "setup_ttl", {})

        sign = 1.0 if long else -1.0
        move_from_arm = (snapshot.price - state.breakout_price) / state.breakout_price * 100.0 * sign
        if move_from_arm < -float(self.settings.get("max_adverse_move_pct", 0.25)):
            del self.states[state.symbol]
            return StrategyEvent("INVALIDATED", state.symbol, state.direction, now_ms, "expansion_failed", {"move_from_arm_pct": move_from_arm})

        state.favorable_extreme = max(state.favorable_extreme, snapshot.price) if long else min(state.favorable_extreme, snapshot.price)
        boundary = float(state.arm_snapshot["prior_high"] if long else state.arm_snapshot["prior_low"])
        boundary_extension = (snapshot.price - boundary) / boundary * 100.0 * sign
        if state.phase == "ARMED" and boundary_extension <= float(self.settings.get("retest_touch_pct", 0.12)):
            state.phase = "PULLBACK"
            micro_extreme = snapshot.micro_low if long else snapshot.micro_high
            state.pullback_extreme = micro_extreme if micro_extreme > 0 else snapshot.price
            state.pullback_candle_ms = snapshot.micro_open_time_ms
            return StrategyEvent("PULLBACK", state.symbol, state.direction, now_ms, "breakout_retest", {"state": asdict(state)})
        if state.phase == "PULLBACK":
            observed_extreme = snapshot.micro_low if long else snapshot.micro_high
            if observed_extreme <= 0:
                observed_extreme = snapshot.price
            state.pullback_extreme = (
                min(float(state.pullback_extreme), observed_extreme)
                if long else max(float(state.pullback_extreme), observed_extreme)
            )
        confirmation = self._confirmation_checks(state, snapshot)
        if not all(confirmation.values()):
            return None

        entry = snapshot.price
        min_stop = float(self.settings["min_stop_pct"])
        max_stop = float(self.settings["max_stop_pct"])
        breakout_boundary = float(
            state.arm_snapshot["prior_high"] if long else state.arm_snapshot["prior_low"]
        )
        buffer_pct = float(self.settings.get("stop_buffer_pct", 0.05)) / 100.0
        buffered_structural = breakout_boundary * (1.0 - buffer_pct if long else 1.0 + buffer_pct)
        stop = min(buffered_structural, entry * (1.0 - min_stop / 100.0)) if long else max(buffered_structural, entry * (1.0 + min_stop / 100.0))
        risk_pct = abs(entry - stop) / entry * 100.0
        if risk_pct > max_stop:
            del self.states[state.symbol]
            return StrategyEvent("INVALIDATED", state.symbol, state.direction, now_ms, "structural_stop_too_wide", {"risk_pct": risk_pct})
        risk = abs(entry - stop)
        target_r = float(self.settings["target_r"])
        target = entry + risk * target_r if long else entry - risk * target_r
        signal = TradeSignal(
            symbol=state.symbol,
            direction=state.direction,
            timestamp_ms=now_ms,
            entry_price=entry,
            stop_price=stop,
            target_price=target,
            risk_pct=risk_pct,
            setup="OI_EXPANSION_CONFIRMATION",
            evidence={
                "arm": state.arm_snapshot,
                "entry": snapshot.to_dict(),
                "confirmation_checks": confirmation,
                "breakout_boundary": breakout_boundary,
                "buffered_structural_stop": buffered_structural,
                "price_extension_from_arm_pct": move_from_arm,
                "oi_acceleration_pct_points": snapshot.oi_change_15m_pct - float(state.arm_snapshot["oi_change_15m_pct"]),
                "entry_model": "RETEST_RECLAIM" if state.phase == "PULLBACK" else "DIRECT_CONFIRMATION",
                "price_oi_regime": "PRICE_DIRECTIONAL_OI_EXPANDING",
                "lsr_context": {
                    "arm": float(state.arm_snapshot["global_lsr"]),
                    "entry": snapshot.global_lsr,
                    "change_5m_pct": snapshot.lsr_change_5m_pct,
                },
            },
        )
        del self.states[state.symbol]
        self.last_signal_ms[state.symbol] = now_ms
        reason = "breakout_retest_reclaim" if state.phase == "PULLBACK" else "oi_price_acceleration"
        return StrategyEvent("SIGNAL", state.symbol, state.direction, now_ms, reason, signal.to_dict())

    def snapshot(self) -> dict[str, Any]:
        return {symbol: asdict(state) for symbol, state in self.states.items()}


class OIExpansionArmProbeStrategy(OIBreakoutRetestStrategy):
    """Small direct probe at the first complete price/OI breakout, before a retest exists."""

    name = "OI_EXPANSION_ARM_PROBE"

    def _cooldown_active(self, symbol: str, now_ms: int) -> bool:
        cooldown_ms = int(float(self.settings.get("arm_probe_cooldown_minutes", 20)) * 60_000)
        last = self.last_signal_ms.get(symbol)
        return last is not None and now_ms - last < cooldown_ms

    def _probe_checks(self, snapshot: MarketSnapshot, direction: str) -> dict[str, bool]:
        checks = self._initial_checks(snapshot, direction)
        impulse = abs(snapshot.price_change_15m_pct)
        checks.update({
            "not_late": impulse <= float(self.settings.get("arm_probe_max_impulse_pct", 1.80)),
            "oi_accelerates": snapshot.oi_acceleration_5m_pct_points >= float(
                self.settings.get("arm_probe_min_oi_acceleration_pct_points", 0.03)
            ),
            "volume_accelerates": snapshot.volume_growth_ratio >= float(
                self.settings.get("arm_probe_min_volume_growth_ratio", 1.0)
            ),
        })
        return checks

    def diagnose(self, snapshot: MarketSnapshot) -> dict[str, Any]:
        direction = "LONG" if snapshot.price_change_15m_pct > 0 else "SHORT"
        checks = self._probe_checks(snapshot, direction)
        return {
            "symbol": snapshot.symbol, "strategy": self.name, "direction": direction,
            "phase": "SCANNING", "checks": checks, "passed": sum(checks.values()),
            "total": len(checks), "blocked_by": [key for key, value in checks.items() if not value],
        }

    def observe(self, snapshot: MarketSnapshot) -> StrategyEvent | None:
        now_ms = snapshot.timestamp_ms or int(time.time() * 1000)
        if self._cooldown_active(snapshot.symbol, now_ms):
            return None
        direction = "LONG" if snapshot.price_change_15m_pct > 0 else "SHORT"
        checks = self._probe_checks(snapshot, direction)
        if not all(checks.values()):
            return None
        long = direction == "LONG"
        entry = snapshot.price
        buffer = float(self.settings.get("arm_probe_structure_buffer_pct", 0.08)) / 100.0
        structural = (
            snapshot.candle_low * (1.0 - buffer)
            if long else snapshot.candle_high * (1.0 + buffer)
        )
        min_stop = float(self.settings.get("arm_probe_min_stop_pct", 0.55)) / 100.0
        stop = min(structural, entry * (1.0 - min_stop)) if long else max(
            structural, entry * (1.0 + min_stop)
        )
        risk_pct = abs(entry - stop) / entry * 100.0 if entry else 0.0
        if risk_pct <= 0 or risk_pct > float(self.settings.get("arm_probe_max_stop_pct", 0.90)):
            return StrategyEvent(
                "INVALIDATED", snapshot.symbol, direction, now_ms,
                "arm_probe_stop_too_wide", {"risk_pct": risk_pct, "checks": checks},
            )
        risk = abs(entry - stop)
        target_r = float(self.settings.get("arm_probe_target_r", 2.0))
        target = entry + risk * target_r if long else entry - risk * target_r
        signal = TradeSignal(
            symbol=snapshot.symbol, direction=direction, timestamp_ms=now_ms,
            entry_price=entry, stop_price=stop, target_price=target, risk_pct=risk_pct,
            setup=self.name,
            evidence={
                "entry": snapshot.to_dict(), "arm_probe_checks": checks,
                "micro_structural_stop": structural,
            },
        )
        self.last_signal_ms[snapshot.symbol] = now_ms
        return StrategyEvent(
            "SIGNAL", snapshot.symbol, direction, now_ms,
            "oi_expansion_arm_probe", signal.to_dict(),
        )

    def snapshot(self) -> dict[str, Any]:
        return {}
