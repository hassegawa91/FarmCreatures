from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from engine.binance import MarketSnapshot
from engine.strategy import StrategyEvent, TradeSignal


@dataclass
class MomentumState:
    symbol: str
    direction: str
    armed_at_ms: int
    expires_at_ms: int
    arm_price: float
    favorable_extreme: float
    pullback_extreme: float | None = None
    pullback_candle_ms: int = 0
    arm_snapshot: dict[str, Any] = field(default_factory=dict)
    phase: str = "MOMENTUM_ARMED"


class MomentumStaircaseStrategy:
    """Join a price/OI/volume staircase on its first confirmed one-minute pullback."""

    name = "OI_MOMENTUM_PULLBACK"

    def __init__(self, settings: dict[str, Any]):
        self.settings = settings
        self.states: dict[str, MomentumState] = {}
        self.last_signal_ms: dict[str, int] = {}

    def _direction(self, snapshot: MarketSnapshot) -> str:
        return "LONG" if snapshot.price_change_15m_pct > 0 else "SHORT"

    def _cooldown_active(self, symbol: str, now_ms: int) -> bool:
        last = self.last_signal_ms.get(symbol)
        cooldown = int(float(self.settings.get("momentum_cooldown_minutes", 20)) * 60_000)
        return last is not None and now_ms - last < cooldown

    def _arm_checks(self, snapshot: MarketSnapshot, direction: str) -> dict[str, bool]:
        cfg = self.settings
        long = direction == "LONG"
        impulse = snapshot.price_change_15m_pct * (1.0 if long else -1.0)
        taker_ok = (
            float(cfg.get("momentum_long_taker_min", 1.15)) <= snapshot.taker_buy_sell_ratio <= float(cfg.get("momentum_long_taker_max", 1.80))
            if long else float(cfg.get("momentum_short_taker_min", 0.55)) <= snapshot.taker_buy_sell_ratio <= float(cfg.get("momentum_short_taker_max", 0.87))
        )
        lsr_trend = snapshot.lsr_change_5m_pct * (1.0 if long else -1.0)
        return {
            "staircase": snapshot.directional_candle_count >= int(cfg.get("momentum_min_directional_candles", 2)),
            "impulse_window": float(cfg.get("momentum_min_impulse_pct", 1.20)) <= impulse <= float(cfg.get("momentum_max_arm_impulse_pct", 3.0)),
            "breakout": snapshot.price > snapshot.prior_high if long else snapshot.price < snapshot.prior_low,
            "oi_positive": snapshot.oi_change_15m_pct >= float(cfg.get("momentum_min_oi_change_pct", 0.05)),
            "oi_accelerates": snapshot.oi_acceleration_5m_pct_points >= float(cfg.get("momentum_min_oi_acceleration_pct_points", 0.08)),
            "volume_present": snapshot.volume_ratio >= float(cfg.get("momentum_min_volume_ratio", 0.75)),
            "volume_accelerates": snapshot.volume_growth_ratio >= float(cfg.get("momentum_min_volume_growth_ratio", 1.15)),
            "taker_directional": taker_ok,
            "lsr_not_worsening": lsr_trend <= float(cfg.get("momentum_max_lsr_deterioration_pct", 0.75)),
            "funding": snapshot.funding_rate_pct <= float(cfg["long_max_funding_pct"]) if long else snapshot.funding_rate_pct >= float(cfg["short_min_funding_pct"]),
            "spread": snapshot.spread_pct <= float(cfg["max_spread_pct"]),
        }

    def _entry_checks(self, state: MomentumState, snapshot: MarketSnapshot) -> dict[str, bool]:
        cfg = self.settings
        long = state.direction == "LONG"
        sign = 1.0 if long else -1.0
        reclaim = (snapshot.micro_close - float(state.pullback_extreme)) / float(state.pullback_extreme) * 100.0 * sign
        extension = (snapshot.price - state.arm_price) / state.arm_price * 100.0 * sign
        micro_reclaim = (
            snapshot.micro_close > snapshot.micro_open and snapshot.micro_close > snapshot.micro_previous_high
            if long else snapshot.micro_close < snapshot.micro_open and snapshot.micro_close < snapshot.micro_previous_low
        )
        taker_ok = (
            float(cfg.get("momentum_entry_long_taker_min", 1.05)) <= snapshot.taker_buy_sell_ratio <= float(cfg.get("momentum_long_taker_max", 1.80))
            if long else float(cfg.get("momentum_short_taker_min", 0.55)) <= snapshot.taker_buy_sell_ratio <= float(cfg.get("momentum_entry_short_taker_max", 0.95))
        )
        arm_lsr = float(state.arm_snapshot.get("global_lsr") or snapshot.global_lsr)
        lsr_change = (snapshot.global_lsr - arm_lsr) / arm_lsr * 100.0 * sign if arm_lsr else 0.0
        return {
            "fresh_micro_candle": snapshot.micro_open_time_ms > state.pullback_candle_ms,
            "micro_reclaim": micro_reclaim,
            "reclaim_distance": reclaim >= float(cfg.get("momentum_min_reclaim_pct", 0.08)),
            "not_chasing": extension <= float(cfg.get("momentum_max_entry_extension_pct", 1.50)),
            "oi_holds": snapshot.oi_change_15m_pct >= float(cfg.get("momentum_entry_min_oi_change_pct", 0.02)),
            "volume_alive": snapshot.volume_ratio >= float(cfg.get("momentum_entry_min_volume_ratio", 0.80)),
            "taker_confirms": taker_ok,
            "lsr_not_worsening": lsr_change <= float(cfg.get("momentum_entry_max_lsr_deterioration_pct", 2.0)),
            "spread": snapshot.spread_pct <= float(cfg["max_spread_pct"]),
        }

    def diagnose(self, snapshot: MarketSnapshot) -> dict[str, Any]:
        state = self.states.get(snapshot.symbol)
        direction = state.direction if state else self._direction(snapshot)
        checks = self._entry_checks(state, snapshot) if state and state.phase == "MOMENTUM_PULLBACK" else self._arm_checks(snapshot, direction)
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
        direction = self._direction(snapshot)
        checks = self._arm_checks(snapshot, direction)
        if not all(checks.values()):
            return None
        ttl = int(float(self.settings.get("momentum_setup_ttl_minutes", 15)) * 60_000)
        long = direction == "LONG"
        state = MomentumState(
            symbol=snapshot.symbol,
            direction=direction,
            armed_at_ms=now_ms,
            expires_at_ms=now_ms + ttl,
            arm_price=snapshot.price,
            favorable_extreme=snapshot.micro_high if long else snapshot.micro_low,
            arm_snapshot=snapshot.to_dict(),
        )
        self.states[snapshot.symbol] = state
        return StrategyEvent(
            "MOMENTUM_ARMED", snapshot.symbol, direction, now_ms, "oi_volume_staircase",
            {"checks": checks, "state": asdict(state)},
        )

    def _advance(self, state: MomentumState, snapshot: MarketSnapshot) -> StrategyEvent | None:
        now_ms = snapshot.timestamp_ms
        long = state.direction == "LONG"
        sign = 1.0 if long else -1.0
        if now_ms >= state.expires_at_ms:
            del self.states[state.symbol]
            return StrategyEvent("EXPIRED", state.symbol, state.direction, now_ms, "momentum_ttl", {})

        state.favorable_extreme = (
            max(state.favorable_extreme, snapshot.micro_high, snapshot.price)
            if long else min(state.favorable_extreme, snapshot.micro_low, snapshot.price)
        )
        retracement = (state.favorable_extreme - snapshot.micro_close) / state.favorable_extreme * 100.0 * sign
        if state.phase == "MOMENTUM_ARMED":
            counter_candle = snapshot.micro_close < snapshot.micro_open if long else snapshot.micro_close > snapshot.micro_open
            if (
                counter_candle
                and snapshot.micro_open_time_ms > int(state.arm_snapshot.get("micro_open_time_ms") or 0)
                and float(self.settings.get("momentum_min_pullback_pct", 0.10)) <= retracement <= float(self.settings.get("momentum_max_pullback_pct", 0.60))
            ):
                state.phase = "MOMENTUM_PULLBACK"
                state.pullback_extreme = snapshot.micro_low if long else snapshot.micro_high
                state.pullback_candle_ms = snapshot.micro_open_time_ms
                return StrategyEvent(
                    "MOMENTUM_PULLBACK", state.symbol, state.direction, now_ms, "micro_pullback",
                    {"state": asdict(state), "retracement_pct": retracement},
                )
            return None

        state.pullback_extreme = (
            min(float(state.pullback_extreme), snapshot.micro_low)
            if long else max(float(state.pullback_extreme), snapshot.micro_high)
        )
        total_pullback = (state.favorable_extreme - float(state.pullback_extreme)) / state.favorable_extreme * 100.0 * sign
        if total_pullback > float(self.settings.get("momentum_invalidation_pullback_pct", 0.80)):
            del self.states[state.symbol]
            return StrategyEvent(
                "INVALIDATED", state.symbol, state.direction, now_ms,
                "momentum_pullback_too_deep", {"pullback_pct": total_pullback},
            )
        checks = self._entry_checks(state, snapshot)
        if not all(checks.values()):
            return None

        entry = snapshot.price
        buffer = float(self.settings.get("momentum_stop_buffer_pct", 0.08)) / 100.0
        structural = float(state.pullback_extreme) * (1.0 - buffer if long else 1.0 + buffer)
        min_stop = float(self.settings.get("momentum_min_stop_pct", 0.30)) / 100.0
        stop = min(structural, entry * (1.0 - min_stop)) if long else max(structural, entry * (1.0 + min_stop))
        risk_pct = abs(entry - stop) / entry * 100.0
        if risk_pct > float(self.settings.get("momentum_max_stop_pct", 0.80)):
            del self.states[state.symbol]
            return StrategyEvent(
                "INVALIDATED", state.symbol, state.direction, now_ms,
                "momentum_stop_too_wide", {"risk_pct": risk_pct},
            )
        risk = abs(entry - stop)
        target_r = float(self.settings.get("momentum_target_r", 2.2))
        target = entry + risk * target_r if long else entry - risk * target_r
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
                "entry_checks": checks,
                "favorable_extreme": state.favorable_extreme,
                "pullback_extreme": state.pullback_extreme,
            },
        )
        del self.states[state.symbol]
        self.last_signal_ms[state.symbol] = now_ms
        return StrategyEvent(
            "SIGNAL", state.symbol, state.direction, now_ms, "momentum_pullback_reclaim", signal.to_dict(),
        )

    def snapshot(self) -> dict[str, Any]:
        return {symbol: asdict(state) for symbol, state in self.states.items()}


class MomentumEarlyStrategy(MomentumStaircaseStrategy):
    """Earlier staircase entry using the latest closed one-minute structure as risk."""

    name = "OI_MOMENTUM_EARLY"

    def __init__(self, settings: dict[str, Any]):
        super().__init__(settings)
        self.states = {}

    def _arm_checks(self, snapshot: MarketSnapshot, direction: str) -> dict[str, bool]:
        checks = super()._arm_checks(snapshot, direction)
        impulse = snapshot.price_change_15m_pct * (1.0 if direction == "LONG" else -1.0)
        checks["impulse_window"] = (
            float(self.settings.get("momentum_min_impulse_pct", 1.20))
            <= impulse
            <= float(self.settings.get("momentum_early_max_impulse_pct", 1.80))
        )
        return checks

    def diagnose(self, snapshot: MarketSnapshot) -> dict[str, Any]:
        direction = self._direction(snapshot)
        checks = self._arm_checks(snapshot, direction)
        return {
            "symbol": snapshot.symbol,
            "strategy": self.name,
            "direction": direction,
            "phase": "SCANNING",
            "checks": checks,
            "passed": sum(checks.values()),
            "total": len(checks),
            "blocked_by": [key for key, value in checks.items() if not value],
        }

    def observe(self, snapshot: MarketSnapshot) -> StrategyEvent | None:
        now_ms = snapshot.timestamp_ms or int(time.time() * 1000)
        if self._cooldown_active(snapshot.symbol, now_ms):
            return None
        direction = self._direction(snapshot)
        checks = self._arm_checks(snapshot, direction)
        if not all(checks.values()):
            return None
        long = direction == "LONG"
        entry = snapshot.price
        buffer = float(self.settings.get("momentum_early_stop_buffer_pct", 0.08)) / 100.0
        structural = snapshot.micro_low * (1.0 - buffer) if long else snapshot.micro_high * (1.0 + buffer)
        min_stop = float(self.settings.get("momentum_early_min_stop_pct", 0.30)) / 100.0
        stop = min(structural, entry * (1.0 - min_stop)) if long else max(structural, entry * (1.0 + min_stop))
        risk_pct = abs(entry - stop) / entry * 100.0
        max_stop = float(self.settings.get("momentum_early_max_stop_pct", 0.80))
        if risk_pct <= 0 or risk_pct > max_stop:
            return StrategyEvent(
                "INVALIDATED", snapshot.symbol, direction, now_ms,
                "momentum_early_stop_too_wide", {"risk_pct": risk_pct, "checks": checks},
            )
        risk = abs(entry - stop)
        target_r = float(self.settings.get("momentum_early_target_r", 2.2))
        target = entry + risk * target_r if long else entry - risk * target_r
        signal = TradeSignal(
            symbol=snapshot.symbol,
            direction=direction,
            timestamp_ms=now_ms,
            entry_price=entry,
            stop_price=stop,
            target_price=target,
            risk_pct=risk_pct,
            setup=self.name,
            evidence={
                "entry": snapshot.to_dict(),
                "arm_checks": checks,
                "micro_structural_stop": structural,
            },
        )
        self.last_signal_ms[snapshot.symbol] = now_ms
        return StrategyEvent(
            "SIGNAL", snapshot.symbol, direction, now_ms, "momentum_early_entry", signal.to_dict(),
        )

    def snapshot(self) -> dict[str, Any]:
        return {}
