from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from engine.binance import MarketSnapshot
from engine.strategy import StrategyEvent, TradeSignal


@dataclass
class BorderState:
    symbol: str
    armed_at_ms: int
    expires_at_ms: int
    last_candle_ms: int
    range_low: float
    range_high: float
    breakout_direction: str = ""
    breakout_boundary: float = 0.0
    breakout_candle_ms: int = 0
    breakout_qualified: bool = False
    breakout_snapshot: dict[str, Any] | None = None


class BorderRegimeStrategy:
    """Mutually exclusive edge fade, confirmed breakout, and failed-breakout reversal."""

    name = "BORDER_REGIME_V1"

    def __init__(self, settings: dict[str, Any]):
        self.settings = settings
        self.states: dict[str, BorderState] = {}
        self.observations: dict[str, dict[str, int]] = {}
        self.last_signal_ms: dict[str, int] = {}

    def _cooldown_active(self, symbol: str, now_ms: int) -> bool:
        last = self.last_signal_ms.get(symbol)
        cooldown = int(float(self.settings.get("border_cooldown_minutes", 30)) * 60_000)
        return last is not None and now_ms - last < cooldown

    def _range_checks(self, snapshot: MarketSnapshot) -> dict[str, bool]:
        width = snapshot.edge_range_width_pct
        return {
            "range_width": float(self.settings.get("border_min_range_width_pct", 0.80)) <= width
            <= float(self.settings.get("border_max_range_width_pct", 3.00)),
            "range_defined": snapshot.edge_range_high > snapshot.edge_range_low > 0,
            "adx_allows": snapshot.adx14 <= float(self.settings.get("border_session_max_adx", 25.0)),
            "oi_not_directional": abs(snapshot.oi_change_15m_pct) <= float(
                self.settings.get("border_session_max_abs_oi_15m_pct", 0.80)
            ),
            "volume_not_climax": snapshot.volume_ratio <= float(
                self.settings.get("border_session_max_volume_ratio", 1.50)
            ),
            "spread": snapshot.spread_pct <= float(self.settings.get("max_spread_pct", 0.08)),
        }

    def _breakout_checks(self, snapshot: MarketSnapshot, direction: str) -> dict[str, bool]:
        long = direction == "LONG"
        return {
            "volume_expands": snapshot.volume_ratio >= float(
                self.settings.get("border_breakout_min_volume_ratio", 1.25)
            ),
            "oi_builds": snapshot.oi_change_15m_pct >= float(
                self.settings.get("border_breakout_min_oi_15m_pct", 0.10)
            ),
            "oi_accelerates": snapshot.oi_acceleration_5m_pct_points >= float(
                self.settings.get("border_breakout_min_oi_acceleration", 0.0)
            ),
            "taker_directional": snapshot.taker_buy_sell_ratio >= float(
                self.settings.get("border_breakout_long_min_taker", 1.03)
            ) if long else snapshot.taker_buy_sell_ratio <= float(
                self.settings.get("border_breakout_short_max_taker", 0.97)
            ),
            "spread": snapshot.spread_pct <= float(self.settings.get("max_spread_pct", 0.08)),
        }

    def _breakout_stop_pct(
        self, state: BorderState, snapshot: MarketSnapshot, direction: str,
    ) -> float | None:
        """Place risk behind the retested boundary/structure without accepting arbitrary width."""
        entry = float(snapshot.price)
        long = direction == "LONG"
        buffer_pct = max(
            float(self.settings.get("border_breakout_structure_buffer_pct", 0.08)),
            float(snapshot.atr14_pct)
            * float(self.settings.get("border_breakout_structure_buffer_atr_fraction", 0.15)),
        )
        if long:
            structure = min(float(snapshot.candle_low), float(state.breakout_boundary))
            raw = (entry - structure) / entry * 100.0 + buffer_pct
        else:
            structure = max(float(snapshot.candle_high), float(state.breakout_boundary))
            raw = (structure - entry) / entry * 100.0 + buffer_pct
        minimum = float(self.settings.get("border_breakout_min_stop_pct", 0.65))
        maximum = float(self.settings.get("border_breakout_max_stop_pct", 1.20))
        return None if raw > maximum else max(minimum, raw)

    def diagnose(self, snapshot: MarketSnapshot) -> dict[str, Any]:
        state = self.states.get(snapshot.symbol)
        if state and state.breakout_direction:
            direction = state.breakout_direction
            checks = self._breakout_checks(snapshot, direction)
            phase = "BORDER_BREAKOUT"
        elif state:
            direction = "LONG" if snapshot.price <= (state.range_low + state.range_high) / 2 else "SHORT"
            checks = self._range_checks(snapshot)
            phase = "BORDER_ARMED"
        else:
            direction = "LONG" if snapshot.edge_position <= 0.5 else "SHORT"
            checks = self._range_checks(snapshot)
            phase = "SCANNING"
        return {
            "symbol": snapshot.symbol, "strategy": self.name, "direction": direction,
            "phase": phase, "checks": checks, "passed": sum(checks.values()),
            "total": len(checks), "blocked_by": [key for key, passed in checks.items() if not passed],
        }

    def _signal(
        self, snapshot: MarketSnapshot, direction: str, setup: str, stop_pct: float,
        target_r: float, reason: str, evidence: dict[str, Any],
    ) -> StrategyEvent:
        entry = float(snapshot.price)
        risk = entry * stop_pct / 100.0
        long = direction == "LONG"
        stop = entry - risk if long else entry + risk
        target = entry + risk * target_r if long else entry - risk * target_r
        signal = TradeSignal(
            symbol=snapshot.symbol, direction=direction, timestamp_ms=int(snapshot.timestamp_ms),
            entry_price=entry, stop_price=stop, target_price=target, risk_pct=stop_pct,
            setup=setup, evidence={**evidence, "entry": snapshot.to_dict(), "target_r": target_r},
        )
        self.states.pop(snapshot.symbol, None)
        self.observations.pop(snapshot.symbol, None)
        self.last_signal_ms[snapshot.symbol] = int(snapshot.timestamp_ms)
        return StrategyEvent("SIGNAL", snapshot.symbol, direction, int(snapshot.timestamp_ms), reason, signal.to_dict())

    def observe(self, snapshot: MarketSnapshot) -> StrategyEvent | None:
        now_ms, candle_ms = int(snapshot.timestamp_ms), int(snapshot.candle_open_time_ms)
        state = self.states.get(snapshot.symbol)
        if state is not None:
            return self._advance(state, snapshot)
        if self._cooldown_active(snapshot.symbol, now_ms):
            return None
        checks = self._range_checks(snapshot)
        valid = all(checks.values())
        observation = self.observations.setdefault(snapshot.symbol, {"count": 0, "candle_ms": 0})
        if candle_ms <= observation["candle_ms"]:
            return None
        observation["candle_ms"] = candle_ms
        observation["count"] = observation["count"] + 1 if valid else 0
        if observation["count"] < int(self.settings.get("border_min_stable_observations", 3)):
            return None
        ttl = int(float(self.settings.get("border_session_ttl_minutes", 120)) * 60_000)
        state = BorderState(
            symbol=snapshot.symbol, armed_at_ms=now_ms, expires_at_ms=now_ms + ttl,
            last_candle_ms=candle_ms, range_low=float(snapshot.edge_range_low),
            range_high=float(snapshot.edge_range_high),
        )
        self.states[snapshot.symbol] = state
        return StrategyEvent(
            "ARMED", snapshot.symbol, "LONG" if snapshot.edge_position <= 0.5 else "SHORT",
            now_ms, "stable_range_armed", {"state": asdict(state), "checks": checks},
        )

    def _advance(self, state: BorderState, snapshot: MarketSnapshot) -> StrategyEvent | None:
        now_ms, candle_ms = int(snapshot.timestamp_ms), int(snapshot.candle_open_time_ms)
        if now_ms >= state.expires_at_ms:
            self.states.pop(state.symbol, None)
            self.observations.pop(state.symbol, None)
            return StrategyEvent("EXPIRED", state.symbol, "", now_ms, "border_session_ttl", {})
        if candle_ms <= state.last_candle_ms:
            return None
        state.last_candle_ms = candle_ms
        low, high = state.range_low, state.range_high
        width = high - low
        if width <= 0:
            self.states.pop(state.symbol, None)
            return StrategyEvent("INVALIDATED", state.symbol, "", now_ms, "invalid_range_geometry", {})

        if state.breakout_direction:
            return self._advance_breakout(state, snapshot)

        buffer = float(self.settings.get("border_breakout_close_buffer_pct", 0.08)) / 100.0
        breakout_direction = "LONG" if snapshot.candle_close > high * (1.0 + buffer) else (
            "SHORT" if snapshot.candle_close < low * (1.0 - buffer) else ""
        )
        if breakout_direction:
            checks = self._breakout_checks(snapshot, breakout_direction)
            state.breakout_direction = breakout_direction
            state.breakout_boundary = high if breakout_direction == "LONG" else low
            state.breakout_candle_ms = candle_ms
            state.breakout_qualified = all(checks.values())
            state.breakout_snapshot = snapshot.to_dict()
            return StrategyEvent(
                "BREAKOUT_DETECTED", state.symbol, breakout_direction, now_ms,
                "qualified_border_breakout" if state.breakout_qualified else "unconfirmed_border_sweep",
                {"state": asdict(state), "checks": checks},
            )

        if not bool(self.settings.get("border_fade_enabled", False)):
            return None

        outer_fraction = float(self.settings.get("border_fade_outer_fraction", 0.18))
        reclaim_fraction = float(self.settings.get("border_fade_reclaim_fraction", 0.06))
        long_near = snapshot.candle_low <= low + width * outer_fraction
        short_near = snapshot.candle_high >= high - width * outer_fraction
        long_reject = snapshot.candle_close >= low + width * reclaim_fraction and snapshot.candle_close > snapshot.candle_open
        short_reject = snapshot.candle_close <= high - width * reclaim_fraction and snapshot.candle_close < snapshot.candle_open
        if long_near and long_reject and snapshot.taker_buy_sell_ratio >= float(
            self.settings.get("border_fade_long_min_taker", 1.0)
        ):
            return self._signal(
                snapshot, "LONG", "RANGE_EDGE_FADE_LONG",
                float(self.settings.get("border_fade_stop_pct", 0.45)),
                float(self.settings.get("border_fade_target_r", 2.20)), "lower_edge_rejection",
                {"range": asdict(state), "edge": "LOWER", "model": "MEAN_REVERSION"},
            )
        if short_near and short_reject and snapshot.taker_buy_sell_ratio <= float(
            self.settings.get("border_fade_short_max_taker", 1.0)
        ):
            return self._signal(
                snapshot, "SHORT", "RANGE_EDGE_FADE_SHORT",
                float(self.settings.get("border_fade_stop_pct", 0.45)),
                float(self.settings.get("border_fade_target_r", 2.20)), "upper_edge_rejection",
                {"range": asdict(state), "edge": "UPPER", "model": "MEAN_REVERSION"},
            )
        return None

    def _advance_breakout(self, state: BorderState, snapshot: MarketSnapshot) -> StrategyEvent | None:
        direction, boundary = state.breakout_direction, state.breakout_boundary
        long = direction == "LONG"
        back_inside = snapshot.candle_close < boundary if long else snapshot.candle_close > boundary
        taker_reversed = snapshot.taker_buy_sell_ratio <= float(
            self.settings.get("border_failed_long_break_max_taker", 0.98)
        ) if long else snapshot.taker_buy_sell_ratio >= float(
            self.settings.get("border_failed_short_break_min_taker", 1.02)
        )
        arm_oi = float((state.breakout_snapshot or {}).get("oi_change_15m_pct") or 0.0)
        oi_deteriorated = (
            snapshot.oi_change_5m_pct <= float(self.settings.get("border_failed_max_oi_5m_pct", 0.02))
            or snapshot.oi_change_15m_pct <= arm_oi - float(
                self.settings.get("border_failed_min_oi_deterioration_points", 0.10)
            )
        )
        if back_inside and taker_reversed and oi_deteriorated:
            reverse = "SHORT" if long else "LONG"
            setup = "FAILED_BREAKOUT_REVERSAL_SHORT" if long else "FAILED_BREAKOUT_REVERSAL_LONG"
            return self._signal(
                snapshot, reverse, setup, float(self.settings.get("border_failed_stop_pct", 0.55)),
                float(self.settings.get("border_failed_target_r", 2.30)), "failed_breakout_reentered_range",
                {"range": asdict(state), "failed_direction": direction, "model": "LIQUIDITY_SWEEP_REVERSAL"},
            )
        if back_inside:
            state.breakout_direction = ""
            state.breakout_boundary = 0.0
            state.breakout_candle_ms = 0
            state.breakout_qualified = False
            state.breakout_snapshot = None
            return StrategyEvent("INVALIDATED", state.symbol, direction, int(snapshot.timestamp_ms),
                                 "breakout_failed_without_reversal_flow", {"range_preserved": True})
        max_wait = int(float(self.settings.get("border_breakout_retest_ttl_minutes", 20)) * 60_000)
        if int(snapshot.timestamp_ms) - state.breakout_candle_ms > max_wait:
            self.states.pop(state.symbol, None)
            return StrategyEvent("EXPIRED", state.symbol, direction, int(snapshot.timestamp_ms),
                                 "breakout_retest_ttl", {})
        tolerance = float(self.settings.get("border_breakout_retest_tolerance_pct", 0.18)) / 100.0
        touched = snapshot.candle_low <= boundary * (1.0 + tolerance) if long else snapshot.candle_high >= boundary * (1.0 - tolerance)
        held = snapshot.candle_close > boundary if long else snapshot.candle_close < boundary
        taker_holds = snapshot.taker_buy_sell_ratio >= float(
            self.settings.get("border_breakout_long_hold_taker", 1.02)
        ) if long else snapshot.taker_buy_sell_ratio <= float(
            self.settings.get("border_breakout_short_hold_taker", 0.98)
        )
        arm_oi = float((state.breakout_snapshot or {}).get("oi_change_15m_pct") or 0.0)
        oi_holds = (
            snapshot.oi_change_15m_pct >= max(
                float(self.settings.get("border_breakout_retest_min_oi_15m_pct", 0.05)),
                arm_oi - float(self.settings.get("border_breakout_retest_max_oi_decay_points", 0.15)),
            )
            and snapshot.oi_acceleration_5m_pct_points >= float(
                self.settings.get("border_breakout_retest_min_oi_acceleration", -0.02)
            )
        )
        volume_holds = snapshot.volume_ratio >= float(
            self.settings.get("border_breakout_retest_min_volume_ratio", 0.80)
        )
        if state.breakout_qualified and touched and held and taker_holds and oi_holds and volume_holds:
            setup = "BORDER_BREAKOUT_RETEST_LONG" if long else "BORDER_BREAKOUT_RETEST_SHORT"
            stop_pct = self._breakout_stop_pct(state, snapshot, direction)
            if stop_pct is None:
                return StrategyEvent(
                    "BLOCKED", state.symbol, direction, int(snapshot.timestamp_ms),
                    "border_structure_stop_too_wide",
                    {"range": asdict(state), "oi_holds": oi_holds, "volume_holds": volume_holds},
                )
            return self._signal(
                snapshot, direction, setup, stop_pct,
                float(self.settings.get("border_breakout_target_r", 2.20)), "border_breakout_retest_held",
                {
                    "range": asdict(state), "model": "TREND_CONTINUATION",
                    "retest_checks": {
                        "touched": touched, "held": held, "taker_holds": taker_holds,
                        "oi_holds": oi_holds, "volume_holds": volume_holds,
                    },
                },
            )
        return None

    def snapshot(self) -> dict[str, Any]:
        return {symbol: asdict(state) for symbol, state in self.states.items()}
