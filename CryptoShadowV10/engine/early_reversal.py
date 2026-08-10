from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from engine.binance import MarketSnapshot
from engine.strategy import StrategyEvent, TradeSignal


@dataclass
class MicroReversalState:
    symbol: str
    direction: str
    armed_at_ms: int
    expires_at_ms: int
    structure_stop: float
    favorable_extreme: float
    pullback_extreme: float | None = None
    pullback_candle_ms: int = 0
    arm_snapshot: dict[str, Any] = field(default_factory=dict)
    phase: str = "MICRO_ARMED"


class MicroReversalProbeStrategy:
    """Arm on the third 1m reversal candle; enter only after a held pullback and reclaim."""

    name = "MICRO_REVERSAL_PROBE"

    def __init__(self, settings: dict[str, Any]):
        self.settings = settings
        self.states: dict[str, MicroReversalState] = {}
        self.last_signal_ms: dict[str, int] = {}

    def _cooldown_active(self, symbol: str, now_ms: int) -> bool:
        last = self.last_signal_ms.get(symbol)
        cooldown = int(float(self.settings.get("early_reversal_cooldown_minutes", 20)) * 60_000)
        return last is not None and now_ms - last < cooldown

    @staticmethod
    def oi_price_state(snapshot: MarketSnapshot) -> str:
        price = "UP" if snapshot.price_change_15m_pct > 0.05 else "DOWN" if snapshot.price_change_15m_pct < -0.05 else "FLAT"
        oi = "UP" if snapshot.oi_change_15m_pct > 0.05 else "DOWN" if snapshot.oi_change_15m_pct < -0.05 else "FLAT"
        return f"PRICE_{price}_OI_{oi}"

    def _flow_checks(self, snapshot: MarketSnapshot, direction: str, entry: bool = False) -> dict[str, bool]:
        cfg = self.settings
        long = direction == "LONG"
        taker_ok = (
            float(cfg.get("early_reversal_entry_long_taker_min" if entry else "early_reversal_long_taker_min", 1.05))
            <= snapshot.taker_buy_sell_ratio
            <= float(cfg.get("early_reversal_long_taker_max", 1.65))
            if long else
            float(cfg.get("early_reversal_short_taker_min", 0.60))
            <= snapshot.taker_buy_sell_ratio
            <= float(cfg.get("early_reversal_entry_short_taker_max" if entry else "early_reversal_short_taker_max", 0.95))
        )
        positioning_ok = (
            snapshot.global_lsr <= float(cfg.get("early_reversal_long_max_lsr", 1.50))
            if long else snapshot.global_lsr >= float(cfg.get("early_reversal_short_min_lsr", 0.70))
        )
        if entry:
            oi_builds = (
                snapshot.oi_change_5m_pct >= float(cfg.get("early_reversal_entry_min_oi_5m_pct", 0.03))
                and snapshot.oi_acceleration_5m_pct_points
                >= float(cfg.get("early_reversal_entry_min_oi_acceleration_pct_points", 0.03))
            )
            liquidation_stabilizes = (
                snapshot.oi_change_15m_pct < 0
                and snapshot.oi_change_5m_pct >= float(cfg.get("early_reversal_entry_oi_5m_floor_pct", -0.03))
                and snapshot.oi_acceleration_5m_pct_points
                >= float(cfg.get("early_reversal_entry_liquidation_acceleration_pct_points", 0.10))
            )
            oi_supports = oi_builds or liquidation_stabilizes
        else:
            oi_supports = (
                snapshot.oi_change_5m_pct >= float(cfg.get("early_reversal_min_oi_5m_pct", 0.0))
                or snapshot.oi_acceleration_5m_pct_points
                >= float(cfg.get("early_reversal_min_oi_acceleration_pct_points", 0.05))
            )
        return {
            "oi_transition": oi_supports,
            "taker_turns": taker_ok,
            "positioning_allows": positioning_ok,
            "volume_alive": snapshot.volume_ratio >= float(cfg.get("early_reversal_entry_min_volume_ratio", 0.80)),
            "funding": snapshot.funding_rate_pct <= float(cfg["long_max_funding_pct"])
            if long else snapshot.funding_rate_pct >= float(cfg["short_min_funding_pct"]),
            "spread": snapshot.spread_pct <= float(cfg["max_spread_pct"]),
        }

    def _arm_checks(self, snapshot: MarketSnapshot, direction: str) -> dict[str, bool]:
        signed_15m = snapshot.price_change_15m_pct * (1.0 if direction == "LONG" else -1.0)
        return {
            "three_candle_turn": bool(direction)
            and snapshot.micro_directional_candle_count
            >= int(self.settings.get("early_reversal_min_directional_candles", 3)),
            "micro_volume_accelerates": snapshot.micro_volume_ratio
            >= float(self.settings.get("early_reversal_min_micro_volume_ratio", 1.10)),
            "not_late": bool(direction)
            and signed_15m <= float(self.settings.get("early_reversal_max_directional_15m_pct", 0.80)),
            **self._flow_checks(snapshot, direction),
        }

    def _entry_checks(self, state: MicroReversalState, snapshot: MarketSnapshot) -> dict[str, bool]:
        long = state.direction == "LONG"
        reclaim = (
            (snapshot.micro_close - float(state.pullback_extreme)) / float(state.pullback_extreme) * 100.0
            if long else
            (float(state.pullback_extreme) - snapshot.micro_close) / float(state.pullback_extreme) * 100.0
        )
        micro_reclaim = (
            snapshot.micro_close > snapshot.micro_open and snapshot.micro_close > snapshot.micro_previous_high
            if long else
            snapshot.micro_close < snapshot.micro_open and snapshot.micro_close < snapshot.micro_previous_low
        )
        return {
            "fresh_reclaim_candle": snapshot.micro_open_time_ms > state.pullback_candle_ms,
            "structure_reclaims": micro_reclaim,
            "reclaim_distance": reclaim >= float(self.settings.get("early_reversal_min_reclaim_pct", 0.08)),
            **self._flow_checks(snapshot, state.direction, entry=True),
        }

    def diagnose(self, snapshot: MarketSnapshot) -> dict[str, Any]:
        state = self.states.get(snapshot.symbol)
        direction = state.direction if state else snapshot.micro_reversal_direction
        checks = (
            self._entry_checks(state, snapshot)
            if state and state.phase == "MICRO_PULLBACK" else
            self._arm_checks(snapshot, direction)
        )
        return {
            "symbol": snapshot.symbol,
            "strategy": self.name,
            "direction": direction or ("LONG" if snapshot.micro_pre_move_pct < 0 else "SHORT"),
            "phase": state.phase if state else "MICRO_TURN" if direction else "SCANNING",
            "checks": checks,
            "passed": sum(checks.values()),
            "total": len(checks),
            "blocked_by": [key for key, value in checks.items() if not value],
            "oi_price_state": self.oi_price_state(snapshot),
        }

    def observe(self, snapshot: MarketSnapshot) -> StrategyEvent | None:
        now_ms = snapshot.timestamp_ms or int(time.time() * 1000)
        state = self.states.get(snapshot.symbol)
        if state:
            return self._advance(state, snapshot)
        if self._cooldown_active(snapshot.symbol, now_ms):
            return None
        direction = snapshot.micro_reversal_direction
        checks = self._arm_checks(snapshot, direction)
        if not direction or not all(checks.values()):
            return None
        long = direction == "LONG"
        ttl = int(float(self.settings.get("early_reversal_setup_ttl_minutes", 20)) * 60_000)
        state = MicroReversalState(
            symbol=snapshot.symbol,
            direction=direction,
            armed_at_ms=now_ms,
            expires_at_ms=now_ms + ttl,
            structure_stop=snapshot.micro_structure_stop,
            favorable_extreme=snapshot.micro_high if long else snapshot.micro_low,
            arm_snapshot=snapshot.to_dict(),
        )
        self.states[snapshot.symbol] = state
        return StrategyEvent(
            "MICRO_ARMED", snapshot.symbol, direction, now_ms, "three_candle_turn_armed",
            {"checks": checks, "state": asdict(state), "oi_price_state": self.oi_price_state(snapshot)},
        )

    def _advance(self, state: MicroReversalState, snapshot: MarketSnapshot) -> StrategyEvent | None:
        now_ms = snapshot.timestamp_ms
        long = state.direction == "LONG"
        if now_ms >= state.expires_at_ms:
            del self.states[state.symbol]
            return StrategyEvent("EXPIRED", state.symbol, state.direction, now_ms, "micro_reversal_ttl", {})
        structure_broken = snapshot.micro_low <= state.structure_stop if long else snapshot.micro_high >= state.structure_stop
        if structure_broken and snapshot.micro_open_time_ms > int(state.arm_snapshot.get("micro_open_time_ms") or 0):
            del self.states[state.symbol]
            return StrategyEvent("INVALIDATED", state.symbol, state.direction, now_ms, "micro_structure_broken", {})
        state.favorable_extreme = (
            max(state.favorable_extreme, snapshot.micro_high) if long
            else min(state.favorable_extreme, snapshot.micro_low)
        )
        if state.phase == "MICRO_ARMED":
            counter = snapshot.micro_close < snapshot.micro_open if long else snapshot.micro_close > snapshot.micro_open
            pullback = (
                (state.favorable_extreme - snapshot.micro_close) / state.favorable_extreme * 100.0
                if long else
                (snapshot.micro_close - state.favorable_extreme) / state.favorable_extreme * 100.0
            )
            if (
                counter
                and snapshot.micro_open_time_ms > int(state.arm_snapshot.get("micro_open_time_ms") or 0)
                and float(self.settings.get("early_reversal_min_pullback_pct", 0.08))
                <= pullback <= float(self.settings.get("early_reversal_max_pullback_pct", 0.65))
            ):
                state.phase = "MICRO_PULLBACK"
                state.pullback_extreme = snapshot.micro_low if long else snapshot.micro_high
                state.pullback_candle_ms = snapshot.micro_open_time_ms
                return StrategyEvent(
                    "MICRO_PULLBACK", state.symbol, state.direction, now_ms, "first_pullback_held",
                    {"state": asdict(state), "pullback_pct": pullback},
                )
            return None
        state.pullback_extreme = (
            min(float(state.pullback_extreme), snapshot.micro_low) if long
            else max(float(state.pullback_extreme), snapshot.micro_high)
        )
        checks = self._entry_checks(state, snapshot)
        if not all(checks.values()):
            return None
        entry = snapshot.price
        buffer = float(self.settings.get("early_reversal_stop_buffer_pct", 0.08)) / 100.0
        raw_structure = min(state.structure_stop, float(state.pullback_extreme)) if long else max(
            state.structure_stop, float(state.pullback_extreme)
        )
        structural = raw_structure * (1.0 - buffer if long else 1.0 + buffer)
        min_stop = float(self.settings.get("early_reversal_min_stop_pct", 0.45)) / 100.0
        stop = min(structural, entry * (1.0 - min_stop)) if long else max(structural, entry * (1.0 + min_stop))
        risk_pct = abs(entry - stop) / entry * 100.0 if entry else 0.0
        if risk_pct <= 0 or risk_pct > float(self.settings.get("early_reversal_max_stop_pct", 1.20)):
            del self.states[state.symbol]
            return StrategyEvent(
                "INVALIDATED", state.symbol, state.direction, now_ms,
                "reclaim_stop_outside_structure", {"risk_pct": risk_pct, "checks": checks},
            )
        risk = abs(entry - stop)
        target_r = float(self.settings.get("early_reversal_target_r", 3.0))
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
                "oi_price_state": self.oi_price_state(snapshot),
                "reversal_structure_stop": raw_structure,
                "pullback_extreme": state.pullback_extreme,
            },
        )
        del self.states[state.symbol]
        self.last_signal_ms[state.symbol] = now_ms
        return StrategyEvent(
            "SIGNAL", state.symbol, state.direction, now_ms, "pullback_reclaim_confirmed", signal.to_dict(),
        )

    def snapshot(self) -> dict[str, Any]:
        return {symbol: asdict(state) for symbol, state in self.states.items()}
