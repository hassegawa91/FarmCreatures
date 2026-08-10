from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from engine.binance import MarketSnapshot
from engine.strategy import StrategyEvent, TradeSignal


@dataclass
class TrendPullbackState:
    symbol: str
    direction: str
    armed_at_ms: int
    expires_at_ms: int
    arm_candle_ms: int
    last_candle_ms: int
    extreme_price: float
    structural_price: float
    phase: str = "TREND_ARMED"
    pullback_candle_ms: int = 0
    arm_snapshot: dict[str, Any] = field(default_factory=dict)


class FlowTrendPullbackStrategy:
    """Join established futures flow only after a real 5m pullback and reclaim."""

    name = "FLOW_TREND_PULLBACK"

    def __init__(self, settings: dict[str, Any]):
        self.settings = settings
        self.states: dict[str, TrendPullbackState] = {}
        self.last_signal_ms: dict[str, int] = {}

    def _direction(self, snapshot: MarketSnapshot) -> str:
        if snapshot.candle_close >= snapshot.ema21:
            return "LONG"
        return "SHORT"

    def _cooldown(self, symbol: str, now_ms: int) -> bool:
        last = self.last_signal_ms.get(symbol)
        interval = int(float(self.settings.get("trend_pullback_cooldown_minutes", 30)) * 60_000)
        return last is not None and now_ms - last < interval

    def _arm_checks(self, snapshot: MarketSnapshot, direction: str) -> dict[str, bool]:
        cfg, long = self.settings, direction == "LONG"
        sign = 1.0 if long else -1.0
        signed_impulse = snapshot.price_change_15m_pct * sign
        return {
            "ema_direction": snapshot.candle_close > snapshot.ema21 if long else snapshot.candle_close < snapshot.ema21,
            "ema_slope": snapshot.ema21_slope_pct >= float(cfg.get("trend_pullback_min_ema_slope_pct", 0.015)) if long else snapshot.ema21_slope_pct <= -float(cfg.get("trend_pullback_min_ema_slope_pct", 0.015)),
            "trend_strength": float(cfg.get("trend_pullback_min_adx", 20.0)) <= snapshot.adx14 <= float(cfg.get("trend_pullback_max_adx", 55.0)),
            "impulse_window": float(cfg.get("trend_pullback_min_impulse_pct", 0.45)) <= signed_impulse <= float(cfg.get("trend_pullback_max_impulse_pct", 1.80)),
            "oi_builds": snapshot.oi_change_15m_pct >= float(cfg.get("trend_pullback_min_arm_oi_15m_pct", 0.08)),
            "volume_alive": snapshot.volume_ratio >= float(cfg.get("trend_pullback_min_arm_volume_ratio", 0.80)),
            "taker_supports": snapshot.taker_buy_sell_ratio >= float(cfg.get("trend_pullback_long_min_taker", 1.02)) if long else snapshot.taker_buy_sell_ratio <= float(cfg.get("trend_pullback_short_max_taker", 0.98)),
            "lsr_not_chasing": snapshot.lsr_change_5m_pct <= float(cfg.get("trend_pullback_long_max_lsr_change_pct", 0.50)) if long else snapshot.lsr_change_5m_pct >= -float(cfg.get("trend_pullback_short_max_lsr_change_pct", 0.50)),
            "funding": snapshot.funding_rate_pct <= float(cfg.get("trend_pullback_long_max_funding_pct", 0.05)) if long else snapshot.funding_rate_pct >= float(cfg.get("trend_pullback_short_min_funding_pct", -0.05)),
            "spread": snapshot.spread_pct <= float(cfg.get("max_spread_pct", 0.08)),
        }

    def _pullback_checks(self, state: TrendPullbackState, snapshot: MarketSnapshot) -> dict[str, bool]:
        long = state.direction == "LONG"
        retracement = (
            (state.extreme_price - snapshot.candle_close) / state.extreme_price * 100.0
            if long else (snapshot.candle_close - state.extreme_price) / state.extreme_price * 100.0
        )
        minimum = max(
            float(self.settings.get("trend_pullback_min_retracement_pct", 0.15)),
            snapshot.atr14_pct * float(self.settings.get("trend_pullback_atr_retracement_fraction", 0.35)),
        )
        ema_tolerance = float(self.settings.get("trend_pullback_ema_tolerance_pct", 0.20)) / 100.0
        return {
            "fresh_candle": snapshot.candle_open_time_ms > state.arm_candle_ms,
            "meaningful_pullback": retracement >= minimum,
            "trend_not_broken": snapshot.candle_close >= snapshot.ema21 * (1.0 - ema_tolerance) if long else snapshot.candle_close <= snapshot.ema21 * (1.0 + ema_tolerance),
            "oi_not_flushed": snapshot.oi_change_15m_pct >= float(self.settings.get("trend_pullback_min_pullback_oi_15m_pct", -0.10)),
        }

    def _reclaim_checks(self, state: TrendPullbackState, snapshot: MarketSnapshot) -> dict[str, bool]:
        cfg, long = self.settings, state.direction == "LONG"
        return {
            "fresh_reclaim_candle": snapshot.candle_open_time_ms > state.pullback_candle_ms,
            "structure_reclaims": snapshot.candle_close > snapshot.previous_candle_high if long else snapshot.candle_close < snapshot.previous_candle_low,
            "directional_close": snapshot.candle_close > snapshot.candle_open if long else snapshot.candle_close < snapshot.candle_open,
            "ema_holds": snapshot.candle_close > snapshot.ema21 if long else snapshot.candle_close < snapshot.ema21,
            "ema_slope_holds": snapshot.ema21_slope_pct > 0 if long else snapshot.ema21_slope_pct < 0,
            "trend_alive": snapshot.adx14 >= float(cfg.get("trend_pullback_min_reclaim_adx", 18.0)),
            "oi_returns": snapshot.oi_change_15m_pct >= float(cfg.get("trend_pullback_min_reclaim_oi_15m_pct", 0.03)),
            "taker_returns": snapshot.taker_buy_sell_ratio >= float(cfg.get("trend_pullback_long_reclaim_taker", 1.02)) if long else snapshot.taker_buy_sell_ratio <= float(cfg.get("trend_pullback_short_reclaim_taker", 0.98)),
            "volume_alive": snapshot.volume_ratio >= float(cfg.get("trend_pullback_min_reclaim_volume_ratio", 0.75)),
            "spread": snapshot.spread_pct <= float(cfg.get("max_spread_pct", 0.08)),
        }

    def diagnose(self, snapshot: MarketSnapshot) -> dict[str, Any]:
        state = self.states.get(snapshot.symbol)
        direction = state.direction if state else self._direction(snapshot)
        if state is None:
            checks, phase = self._arm_checks(snapshot, direction), "SCANNING"
        elif state.phase == "TREND_PULLBACK":
            checks, phase = self._reclaim_checks(state, snapshot), state.phase
        else:
            checks, phase = self._pullback_checks(state, snapshot), state.phase
        return {
            "symbol": snapshot.symbol, "strategy": self.name, "direction": direction,
            "phase": phase, "checks": checks, "passed": sum(checks.values()), "total": len(checks),
            "blocked_by": [key for key, value in checks.items() if not value],
        }

    def observe(self, snapshot: MarketSnapshot) -> StrategyEvent | None:
        now_ms = int(snapshot.timestamp_ms or time.time() * 1000)
        state = self.states.get(snapshot.symbol)
        if state:
            return self._advance(state, snapshot)
        if self._cooldown(snapshot.symbol, now_ms):
            return None
        direction = self._direction(snapshot)
        checks = self._arm_checks(snapshot, direction)
        if not all(checks.values()):
            return None
        ttl = int(float(self.settings.get("trend_pullback_setup_ttl_minutes", 45)) * 60_000)
        long = direction == "LONG"
        state = TrendPullbackState(
            snapshot.symbol, direction, now_ms, now_ms + ttl,
            snapshot.candle_open_time_ms, snapshot.candle_open_time_ms,
            snapshot.candle_high if long else snapshot.candle_low,
            snapshot.candle_low if long else snapshot.candle_high,
            arm_snapshot=snapshot.to_dict(),
        )
        self.states[snapshot.symbol] = state
        return StrategyEvent("TREND_ARMED", snapshot.symbol, direction, now_ms, "trend_flow_waiting_pullback", {"checks": checks, "state": asdict(state)})

    def _advance(self, state: TrendPullbackState, snapshot: MarketSnapshot) -> StrategyEvent | None:
        now_ms = int(snapshot.timestamp_ms)
        if now_ms >= state.expires_at_ms:
            del self.states[state.symbol]
            return StrategyEvent("EXPIRED", state.symbol, state.direction, now_ms, "trend_pullback_ttl", {})
        if snapshot.candle_open_time_ms <= state.last_candle_ms:
            return None
        state.last_candle_ms = snapshot.candle_open_time_ms
        long = state.direction == "LONG"
        ema_break = float(self.settings.get("trend_pullback_invalidation_below_ema_pct", 0.35)) / 100.0
        broken = snapshot.candle_close < snapshot.ema21 * (1.0 - ema_break) if long else snapshot.candle_close > snapshot.ema21 * (1.0 + ema_break)
        if broken:
            del self.states[state.symbol]
            return StrategyEvent("INVALIDATED", state.symbol, state.direction, now_ms, "trend_structure_broken", {})
        if state.phase == "TREND_ARMED":
            state.extreme_price = max(state.extreme_price, snapshot.candle_high) if long else min(state.extreme_price, snapshot.candle_low)
            checks = self._pullback_checks(state, snapshot)
            if not all(checks.values()):
                return None
            state.phase = "TREND_PULLBACK"
            state.pullback_candle_ms = snapshot.candle_open_time_ms
            state.structural_price = snapshot.candle_low if long else snapshot.candle_high
            return StrategyEvent("TREND_PULLBACK", state.symbol, state.direction, now_ms, "pullback_waiting_flow_reclaim", {"checks": checks, "state": asdict(state)})
        state.structural_price = min(state.structural_price, snapshot.candle_low) if long else max(state.structural_price, snapshot.candle_high)
        checks = self._reclaim_checks(state, snapshot)
        if not all(checks.values()):
            return None
        entry = snapshot.price
        buffer = max(snapshot.atr14 * float(self.settings.get("trend_pullback_stop_atr_buffer", 0.15)), entry * float(self.settings.get("trend_pullback_stop_min_buffer_pct", 0.10)) / 100.0)
        stop = state.structural_price - buffer if long else state.structural_price + buffer
        risk_pct = abs(entry - stop) / entry * 100.0 if entry else 0.0
        if risk_pct <= 0 or risk_pct > float(self.settings.get("trend_pullback_max_stop_pct", 1.50)):
            del self.states[state.symbol]
            return StrategyEvent("INVALIDATED", state.symbol, state.direction, now_ms, "trend_pullback_stop_too_wide", {"risk_pct": risk_pct})
        risk = abs(entry - stop)
        target_r = float(self.settings.get("trend_pullback_target_r", 2.20))
        target = entry + risk * target_r if long else entry - risk * target_r
        signal = TradeSignal(
            state.symbol, state.direction, now_ms, entry, stop, target, risk_pct, self.name,
            {"arm": state.arm_snapshot, "entry": snapshot.to_dict(), "confirmation_checks": checks,
             "pullback_structure": state.structural_price, "trend_extreme": state.extreme_price},
        )
        del self.states[state.symbol]
        self.last_signal_ms[state.symbol] = now_ms
        return StrategyEvent("SIGNAL", state.symbol, state.direction, now_ms, "trend_pullback_reclaimed", signal.to_dict())

    def snapshot(self) -> dict[str, Any]:
        return {symbol: asdict(state) for symbol, state in self.states.items()}
