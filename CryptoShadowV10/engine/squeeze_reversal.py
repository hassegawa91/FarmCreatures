from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from engine.binance import MarketSnapshot
from engine.strategy import StrategyEvent, TradeSignal


@dataclass
class SqueezeState:
    symbol: str
    direction: str
    armed_at_ms: int
    expires_at_ms: int
    extreme_price: float
    arm_oi_change_pct: float
    boundary: float
    arm_snapshot: dict[str, Any] = field(default_factory=dict)
    phase: str = "SQUEEZE_ARMED"


class PostSqueezeReversalStrategy:
    """Fade a crowded impulse only after price, flow and OI confirm exhaustion."""

    name = "POST_SQUEEZE_REVERSAL"

    def __init__(self, settings: dict[str, Any]):
        self.settings = settings
        self.states: dict[str, SqueezeState] = {}
        self.last_signal_ms: dict[str, int] = {}

    def _cooldown_active(self, symbol: str, now_ms: int) -> bool:
        last = self.last_signal_ms.get(symbol)
        cooldown = int(float(self.settings.get("squeeze_cooldown_minutes", 30)) * 60_000)
        return last is not None and now_ms - last < cooldown

    def _arm_direction(self, snapshot: MarketSnapshot) -> str:
        return "SHORT" if snapshot.price_change_15m_pct > 0 else "LONG"

    def _arm_checks(self, snapshot: MarketSnapshot, direction: str) -> dict[str, bool]:
        cfg = self.settings
        short = direction == "SHORT"
        impulse = snapshot.price_change_15m_pct
        breakout = snapshot.price > snapshot.prior_high if short else snapshot.price < snapshot.prior_low
        taker_climax = (
            snapshot.taker_buy_sell_ratio >= float(cfg.get("squeeze_up_taker_climax", 1.55))
            if short else snapshot.taker_buy_sell_ratio <= float(cfg.get("squeeze_down_taker_climax", 0.68))
        )
        crowding = (
            snapshot.global_lsr >= float(cfg.get("squeeze_up_min_lsr", 1.45))
            if short else snapshot.global_lsr <= float(cfg.get("squeeze_down_max_lsr", 0.80))
        )
        return {
            "impulse": impulse >= float(cfg.get("squeeze_min_price_impulse_pct", 1.0))
            if short else impulse <= -float(cfg.get("squeeze_min_price_impulse_pct", 1.0)),
            "breakout": breakout,
            "oi_expansion": snapshot.oi_change_15m_pct >= float(cfg.get("squeeze_min_oi_change_pct", 0.25)),
            "volume_climax": snapshot.volume_ratio >= float(cfg.get("squeeze_min_volume_ratio", 1.6)),
            "positioning_extreme": taker_climax or crowding,
            "spread": snapshot.spread_pct <= float(cfg["max_spread_pct"]),
        }

    def _confirmation_checks(self, state: SqueezeState, snapshot: MarketSnapshot) -> dict[str, bool]:
        cfg = self.settings
        short = state.direction == "SHORT"
        sign = -1.0 if short else 1.0
        closed_price = snapshot.candle_close or snapshot.price
        reversal = (closed_price - state.extreme_price) / state.extreme_price * 100.0 * sign
        squeeze_distance = abs(state.extreme_price - state.boundary)
        retracement_fraction = abs(closed_price - state.extreme_price) / squeeze_distance if squeeze_distance else 0.0
        arm_oi_timestamp = int(state.arm_snapshot.get("oi_timestamp_ms") or 0)
        fresh_oi = snapshot.oi_timestamp_ms > arm_oi_timestamp if arm_oi_timestamp else (
            snapshot.timestamp_ms - state.armed_at_ms >= int(float(cfg.get("squeeze_confirmation_minutes", 5)) * 60_000)
        )
        taker_flip = (
            snapshot.taker_buy_sell_ratio <= float(cfg.get("squeeze_short_confirm_taker", 0.95))
            if short else snapshot.taker_buy_sell_ratio >= float(cfg.get("squeeze_long_confirm_taker", 1.05))
        )
        candle_body_reverses = (
            closed_price < snapshot.candle_open and closed_price < snapshot.previous_candle_close
            if short else closed_price > snapshot.candle_open and closed_price > snapshot.previous_candle_close
        )
        candle_range = max(0.0, snapshot.candle_high - snapshot.candle_low)
        close_location = (
            (closed_price - snapshot.candle_low) / candle_range if candle_range else 0.5
        )
        decisive_close = close_location <= float(cfg.get("squeeze_short_max_close_location", 0.45)) if short else close_location >= float(cfg.get("squeeze_long_min_close_location", 0.55))
        return {
            "fresh_window": snapshot.timestamp_ms - state.armed_at_ms >= int(float(cfg.get("squeeze_confirmation_minutes", 5)) * 60_000),
            "fresh_oi": fresh_oi,
            "fresh_closed_candle": snapshot.candle_open_time_ms > int(state.arm_snapshot.get("candle_open_time_ms") or 0),
            "price_reversal": reversal >= float(cfg.get("squeeze_min_reversal_pct", 0.25)),
            "meaningful_retracement": retracement_fraction >= float(cfg.get("squeeze_min_retracement_fraction", 0.25)),
            "structure_reverses": candle_body_reverses and decisive_close,
            "oi_exhausts": snapshot.oi_change_5m_pct <= float(cfg.get("squeeze_max_oi_change_5m_pct", 0.05)),
            "taker_flips": taker_flip,
            "price_flow_confirms": not taker_flip or candle_body_reverses,
            "volume_alive": snapshot.volume_ratio >= float(cfg.get("squeeze_min_confirmation_volume_ratio", 1.0)),
            "spread": snapshot.spread_pct <= float(cfg["max_spread_pct"]),
        }

    def diagnose(self, snapshot: MarketSnapshot) -> dict[str, Any]:
        state = self.states.get(snapshot.symbol)
        direction = state.direction if state else self._arm_direction(snapshot)
        checks = self._confirmation_checks(state, snapshot) if state else self._arm_checks(snapshot, direction)
        return {
            "symbol": snapshot.symbol,
            "strategy": self.name,
            "direction": direction,
            "phase": state.phase if state else "SCANNING",
            "checks": checks,
            "passed": sum(checks.values()),
            "total": len(checks),
            "blocked_by": [key for key, value in checks.items() if not value],
        }

    def observe(self, snapshot: MarketSnapshot) -> StrategyEvent | None:
        now_ms = snapshot.timestamp_ms or int(time.time() * 1000)
        state = self.states.get(snapshot.symbol)
        if state:
            return self._advance(state, snapshot)
        if self._cooldown_active(snapshot.symbol, now_ms):
            return None
        direction = self._arm_direction(snapshot)
        checks = self._arm_checks(snapshot, direction)
        if not all(checks.values()):
            return None
        ttl = int(float(self.settings.get("squeeze_setup_ttl_minutes", 25)) * 60_000)
        state = SqueezeState(
            symbol=snapshot.symbol,
            direction=direction,
            armed_at_ms=now_ms,
            expires_at_ms=now_ms + ttl,
            extreme_price=snapshot.candle_high if direction == "SHORT" else snapshot.candle_low,
            arm_oi_change_pct=snapshot.oi_change_15m_pct,
            boundary=snapshot.prior_high if direction == "SHORT" else snapshot.prior_low,
            arm_snapshot=snapshot.to_dict(),
        )
        self.states[snapshot.symbol] = state
        return StrategyEvent(
            "SQUEEZE_ARMED", snapshot.symbol, direction, now_ms, "crowded_impulse",
            {"checks": checks, "state": asdict(state)},
        )

    def _advance(self, state: SqueezeState, snapshot: MarketSnapshot) -> StrategyEvent | None:
        now_ms = snapshot.timestamp_ms
        short = state.direction == "SHORT"
        if now_ms >= state.expires_at_ms:
            del self.states[state.symbol]
            return StrategyEvent("EXPIRED", state.symbol, state.direction, now_ms, "squeeze_ttl", {})

        closed_extreme = snapshot.candle_high if short else snapshot.candle_low
        new_extreme = closed_extreme > state.extreme_price if short else closed_extreme < state.extreme_price
        if new_extreme:
            state.extreme_price = closed_extreme
        checks = self._confirmation_checks(state, snapshot)
        if not all(checks.values()):
            return None

        entry = snapshot.price
        buffer = float(self.settings.get("squeeze_stop_buffer_pct", 0.15)) / 100.0
        stop = state.extreme_price * (1.0 + buffer if short else 1.0 - buffer)
        risk_pct = abs(entry - stop) / entry * 100.0
        if risk_pct <= 0 or risk_pct > float(self.settings.get("squeeze_max_stop_pct", 0.90)):
            del self.states[state.symbol]
            return StrategyEvent(
                "INVALIDATED", state.symbol, state.direction, now_ms,
                "squeeze_stop_too_wide", {"risk_pct": risk_pct},
            )
        risk = abs(entry - stop)
        target_r = float(self.settings.get("squeeze_target_r", 2.2))
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
                "squeeze_extreme": state.extreme_price,
                "oi_change_from_arm_pct_points": snapshot.oi_change_15m_pct - state.arm_oi_change_pct,
            },
        )
        del self.states[state.symbol]
        self.last_signal_ms[state.symbol] = now_ms
        return StrategyEvent(
            "SIGNAL", state.symbol, state.direction, now_ms, "post_squeeze_reversal", signal.to_dict(),
        )

    def snapshot(self) -> dict[str, Any]:
        return {symbol: asdict(state) for symbol, state in self.states.items()}
