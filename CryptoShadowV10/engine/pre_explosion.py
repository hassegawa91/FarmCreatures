from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from engine.binance import MarketSnapshot
from engine.strategy import StrategyEvent, TradeSignal


@dataclass
class PreExplosionState:
    symbol: str
    direction: str
    armed_at_ms: int
    expires_at_ms: int
    boundary: float
    arm_price: float
    arm_oi_change_pct: float
    arm_snapshot: dict[str, Any] = field(default_factory=dict)
    phase: str = "PRE_ARMED"


class PreExplosionReversalStrategy:
    """Compression near a boundary, derivatives buildup, then a confirmed breakout."""

    name = "PRE_EXPLOSION_REVERSAL"

    def __init__(self, settings: dict[str, Any]):
        self.settings = settings
        self.states: dict[str, PreExplosionState] = {}
        self.last_signal_ms: dict[str, int] = {}

    def _direction(self, snapshot: MarketSnapshot) -> str:
        high_distance = abs(snapshot.distance_to_prior_high_pct)
        low_distance = abs(snapshot.distance_to_prior_low_pct)
        return "LONG" if high_distance <= low_distance else "SHORT"

    def _cooldown_active(self, symbol: str, now_ms: int) -> bool:
        last = self.last_signal_ms.get(symbol)
        cooldown = int(float(self.settings.get("pre_explosion_cooldown_minutes", 15)) * 60_000)
        return last is not None and now_ms - last < cooldown

    def _pre_checks(self, snapshot: MarketSnapshot, direction: str) -> dict[str, bool]:
        cfg = self.settings
        long = direction == "LONG"
        distance = snapshot.distance_to_prior_high_pct if long else snapshot.distance_to_prior_low_pct
        taker_min = float(cfg.get("pre_explosion_long_pre_taker_min", 0.95)) if long else float(cfg.get("pre_explosion_short_pre_taker_min", 0.65))
        taker_max = float(cfg.get("pre_explosion_long_pre_taker_max", 1.60)) if long else float(cfg.get("pre_explosion_short_pre_taker_max", 1.05))
        return {
            "compression": snapshot.compression_ratio <= float(cfg.get("pre_explosion_max_compression_ratio", 0.80)),
            "range_compact": snapshot.range_width_pct <= float(cfg.get("pre_explosion_max_range_width_pct", 1.50)),
            "near_boundary": -float(cfg.get("pre_explosion_max_prearm_extension_pct", 0.08)) <= distance <= float(cfg.get("pre_explosion_near_boundary_pct", 0.25)),
            "oi_buildup": snapshot.oi_change_15m_pct >= float(cfg.get("pre_explosion_min_oi_change_pct", 0.10)),
            "volume_alive": snapshot.volume_ratio >= float(cfg.get("pre_explosion_min_pre_volume_ratio", 0.80)),
            "taker_balanced": taker_min <= snapshot.taker_buy_sell_ratio <= taker_max,
            "crowding": snapshot.global_lsr <= float(cfg["long_max_global_lsr"]) if long else snapshot.global_lsr >= float(cfg["short_min_global_lsr"]),
            "funding": snapshot.funding_rate_pct <= float(cfg["long_max_funding_pct"]) if long else snapshot.funding_rate_pct >= float(cfg["short_min_funding_pct"]),
            "spread": snapshot.spread_pct <= float(cfg["max_spread_pct"]),
        }

    def _trigger_checks(self, state: PreExplosionState, snapshot: MarketSnapshot) -> dict[str, bool]:
        cfg = self.settings
        long = state.direction == "LONG"
        sign = 1.0 if long else -1.0
        extension = (snapshot.price - state.boundary) / state.boundary * 100.0 * sign
        price_acceleration = (snapshot.price - state.arm_price) / state.arm_price * 100.0 * sign
        oi_acceleration = snapshot.oi_change_15m_pct - state.arm_oi_change_pct
        absorption_threshold = float(cfg.get("pre_explosion_absorption_oi_acceleration_pct_points", 0.08))
        oi_price_efficiency = price_acceleration / oi_acceleration if oi_acceleration >= absorption_threshold else 999.0
        min_oi_hold = max(
            float(cfg.get("pre_explosion_min_oi_change_pct", 0.10)),
            state.arm_oi_change_pct - float(cfg.get("pre_explosion_oi_hold_tolerance_pct_points", 0.05)),
        )
        taker_ok = snapshot.taker_buy_sell_ratio >= float(cfg.get("pre_explosion_long_trigger_taker", 1.05)) if long else snapshot.taker_buy_sell_ratio <= float(cfg.get("pre_explosion_short_trigger_taker", 0.95))
        taker_not_climax = snapshot.taker_buy_sell_ratio <= float(cfg.get("long_max_taker_ratio", 1.8)) if long else snapshot.taker_buy_sell_ratio >= float(cfg.get("short_min_taker_ratio", 0.55))
        return {
            "fresh_observation": snapshot.timestamp_ms > state.armed_at_ms,
            "breakout": extension >= float(cfg.get("pre_explosion_trigger_buffer_pct", 0.05)),
            "not_chasing": extension <= float(cfg.get("pre_explosion_max_trigger_extension_pct", 0.30)),
            "price_accelerates": price_acceleration >= float(cfg.get("pre_explosion_min_price_acceleration_pct", 0.08)),
            "directional_impulse": snapshot.price_change_15m_pct >= float(cfg.get("pre_explosion_min_directional_impulse_pct", 0.0)) if long else snapshot.price_change_15m_pct <= -float(cfg.get("pre_explosion_min_directional_impulse_pct", 0.0)),
            "oi_holds": snapshot.oi_change_15m_pct >= min_oi_hold,
            "price_oi_efficiency": oi_price_efficiency >= float(cfg.get("pre_explosion_min_price_oi_efficiency", 0.50)),
            "compression_valid": snapshot.compression_ratio <= float(cfg.get("pre_explosion_max_trigger_compression_ratio", 1.0)),
            "volume_accelerates": snapshot.volume_ratio >= float(cfg.get("pre_explosion_min_trigger_volume_ratio", 1.10)),
            "taker_confirms": taker_ok,
            "taker_not_climax": taker_not_climax,
            "spread": snapshot.spread_pct <= float(cfg["max_spread_pct"]),
        }

    def diagnose(self, snapshot: MarketSnapshot) -> dict[str, Any]:
        state = self.states.get(snapshot.symbol)
        direction = state.direction if state else self._direction(snapshot)
        checks = self._trigger_checks(state, snapshot) if state else self._pre_checks(snapshot, direction)
        return {
            "symbol": snapshot.symbol, "strategy": self.name, "direction": direction,
            "phase": state.phase if state else "SCANNING", "checks": checks,
            "passed": sum(checks.values()), "total": len(checks),
            "blocked_by": [key for key, value in checks.items() if not value],
        }

    def observe(self, snapshot: MarketSnapshot) -> StrategyEvent | None:
        now_ms = snapshot.timestamp_ms or int(time.time() * 1000)
        state = self.states.get(snapshot.symbol)
        if state:
            return self._advance(state, snapshot)
        if self._cooldown_active(snapshot.symbol, now_ms):
            return None
        direction = self._direction(snapshot)
        checks = self._pre_checks(snapshot, direction)
        if not all(checks.values()):
            return None
        long = direction == "LONG"
        boundary = snapshot.prior_high if long else snapshot.prior_low
        ttl = int(float(self.settings.get("pre_explosion_setup_ttl_minutes", 20)) * 60_000)
        state = PreExplosionState(
            symbol=snapshot.symbol, direction=direction, armed_at_ms=now_ms,
            expires_at_ms=now_ms + ttl, boundary=boundary, arm_price=snapshot.price,
            arm_oi_change_pct=snapshot.oi_change_15m_pct, arm_snapshot=snapshot.to_dict(),
        )
        self.states[snapshot.symbol] = state
        return StrategyEvent("PRE_ARMED", snapshot.symbol, direction, now_ms, "compression_oi_buildup", {"checks": checks, "state": asdict(state)})

    def _advance(self, state: PreExplosionState, snapshot: MarketSnapshot) -> StrategyEvent | None:
        now_ms = snapshot.timestamp_ms
        long = state.direction == "LONG"
        sign = 1.0 if long else -1.0
        if now_ms >= state.expires_at_ms:
            del self.states[state.symbol]
            return StrategyEvent("EXPIRED", state.symbol, state.direction, now_ms, "pre_explosion_ttl", {})
        away = (snapshot.price - state.arm_price) / state.arm_price * 100.0 * sign
        if away < -float(self.settings.get("pre_explosion_max_move_away_pct", 0.35)):
            del self.states[state.symbol]
            return StrategyEvent("INVALIDATED", state.symbol, state.direction, now_ms, "moved_away_from_boundary", {"move_pct": away})
        checks = self._trigger_checks(state, snapshot)
        if not all(checks.values()):
            return None

        entry = snapshot.price
        effective_boundary = state.boundary
        stop_pct = float(self.settings.get("pre_explosion_stop_behind_boundary_pct", 0.35)) / 100.0
        stop = effective_boundary * (1.0 - stop_pct if long else 1.0 + stop_pct)
        risk_pct = abs(entry - stop) / entry * 100.0
        max_risk = float(self.settings.get("pre_explosion_max_stop_pct", 0.65))
        if risk_pct <= 0 or risk_pct > max_risk:
            del self.states[state.symbol]
            return StrategyEvent("INVALIDATED", state.symbol, state.direction, now_ms, "pre_explosion_stop_too_wide", {"risk_pct": risk_pct})
        risk = abs(entry - stop)
        target_r = float(self.settings.get("pre_explosion_target_r", 2.5))
        target = entry + risk * target_r if long else entry - risk * target_r
        signal = TradeSignal(
            symbol=state.symbol, direction=state.direction, timestamp_ms=now_ms,
            entry_price=entry, stop_price=stop, target_price=target, risk_pct=risk_pct,
            setup=self.name,
            evidence={
                "arm": state.arm_snapshot, "entry": snapshot.to_dict(),
                "trigger_checks": checks, "armed_boundary": state.boundary,
                "effective_boundary": effective_boundary,
                "price_acceleration_from_arm_pct": (snapshot.price - state.arm_price) / state.arm_price * 100.0 * (1.0 if long else -1.0),
                "oi_acceleration_pct_points": snapshot.oi_change_15m_pct - state.arm_oi_change_pct,
            },
        )
        del self.states[state.symbol]
        self.last_signal_ms[state.symbol] = now_ms
        return StrategyEvent("SIGNAL", state.symbol, state.direction, now_ms, "pre_explosion_breakout", signal.to_dict())

    def snapshot(self) -> dict[str, Any]:
        return {symbol: asdict(state) for symbol, state in self.states.items()}


class PreExplosionAnticipationStrategy(PreExplosionReversalStrategy):
    """Small position during valid compression/OI buildup, before the boundary breaks."""

    name = "PRE_EXPLOSION_ANTICIPATION"

    def _cooldown_active(self, symbol: str, now_ms: int) -> bool:
        last = self.last_signal_ms.get(symbol)
        cooldown = int(float(self.settings.get("anticipation_cooldown_minutes", 15)) * 60_000)
        return last is not None and now_ms - last < cooldown

    def diagnose(self, snapshot: MarketSnapshot) -> dict[str, Any]:
        direction = self._direction(snapshot)
        checks = self._pre_checks(snapshot, direction)
        return {
            "symbol": snapshot.symbol, "strategy": self.name, "direction": direction,
            "phase": "SCANNING", "checks": checks, "passed": sum(checks.values()),
            "total": len(checks), "blocked_by": [key for key, value in checks.items() if not value],
        }

    def observe(self, snapshot: MarketSnapshot) -> StrategyEvent | None:
        now_ms = snapshot.timestamp_ms or int(time.time() * 1000)
        if self._cooldown_active(snapshot.symbol, now_ms):
            return None
        direction = self._direction(snapshot)
        checks = self._pre_checks(snapshot, direction)
        if not all(checks.values()):
            return None
        long = direction == "LONG"
        entry = snapshot.price
        buffer = float(self.settings.get("anticipation_structure_buffer_pct", 0.08)) / 100.0
        structural = (
            snapshot.candle_low * (1.0 - buffer)
            if long else snapshot.candle_high * (1.0 + buffer)
        )
        min_stop = float(self.settings.get("anticipation_min_stop_pct", 0.45)) / 100.0
        stop = min(structural, entry * (1.0 - min_stop)) if long else max(
            structural, entry * (1.0 + min_stop)
        )
        risk_pct = abs(entry - stop) / entry * 100.0 if entry else 0.0
        if risk_pct <= 0 or risk_pct > float(self.settings.get("anticipation_max_stop_pct", 0.90)):
            return StrategyEvent(
                "INVALIDATED", snapshot.symbol, direction, now_ms,
                "anticipation_stop_too_wide", {"risk_pct": risk_pct, "checks": checks},
            )
        risk = abs(entry - stop)
        target_r = float(self.settings.get("anticipation_target_r", 2.2))
        target = entry + risk * target_r if long else entry - risk * target_r
        signal = TradeSignal(
            symbol=snapshot.symbol, direction=direction, timestamp_ms=now_ms,
            entry_price=entry, stop_price=stop, target_price=target, risk_pct=risk_pct,
            setup=self.name,
            evidence={
                "entry": snapshot.to_dict(), "anticipation_checks": checks,
                "boundary": snapshot.prior_high if long else snapshot.prior_low,
                "micro_structural_stop": structural,
            },
        )
        self.last_signal_ms[snapshot.symbol] = now_ms
        return StrategyEvent(
            "SIGNAL", snapshot.symbol, direction, now_ms,
            "compression_oi_anticipation", signal.to_dict(),
        )

    def snapshot(self) -> dict[str, Any]:
        return {}
