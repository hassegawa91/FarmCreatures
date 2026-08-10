import unittest

from engine.binance import MarketSnapshot
from engine.early_reversal import MicroReversalProbeStrategy


SETTINGS = {
    "long_max_funding_pct": 0.05,
    "short_min_funding_pct": -0.05,
    "max_spread_pct": 0.08,
    "early_reversal_min_directional_candles": 3,
    "early_reversal_min_micro_volume_ratio": 1.10,
    "early_reversal_max_directional_15m_pct": 0.80,
    "early_reversal_min_oi_5m_pct": 0.0,
    "early_reversal_min_oi_acceleration_pct_points": 0.05,
    "early_reversal_long_taker_min": 1.05,
    "early_reversal_long_taker_max": 1.65,
    "early_reversal_short_taker_min": 0.60,
    "early_reversal_short_taker_max": 0.95,
    "early_reversal_long_max_lsr": 1.50,
    "early_reversal_short_min_lsr": 0.70,
    "early_reversal_min_stop_pct": 0.45,
    "early_reversal_max_stop_pct": 1.20,
    "early_reversal_target_r": 3.0,
}


def snapshot(direction="LONG", **changes):
    long = direction == "LONG"
    row = dict(
        symbol="TESTUSDT", timestamp_ms=1_000_000, price=100.0,
        price_change_15m_pct=-0.40 if long else 0.40,
        oi_change_15m_pct=0.10, oi_change_5m_pct=0.03,
        oi_acceleration_5m_pct_points=0.08,
        global_lsr=0.90 if long else 1.30,
        taker_buy_sell_ratio=1.20 if long else 0.80,
        funding_rate_pct=0.0, volume_ratio=1.0, spread_pct=0.02,
        prior_high=101.0, prior_low=99.0, candle_high=100.2, candle_low=99.4,
        previous_candle_high=100.1, previous_candle_low=99.2,
        micro_open=99.8 if long else 100.2, micro_high=100.1, micro_low=99.7,
        micro_close=100.0, micro_previous_high=99.9, micro_previous_low=100.1,
        micro_open_time_ms=1_000_000,
        micro_reversal_direction=direction, micro_directional_candle_count=3,
        micro_reversal_impulse_pct=0.35 if long else -0.35,
        micro_pre_move_pct=-0.60 if long else 0.60,
        micro_volume_ratio=1.40, micro_structure_stop=99.4 if long else 100.6,
    )
    row.update(changes)
    return MarketSnapshot(**row)


class MicroReversalProbeTests(unittest.TestCase):
    def test_long_turn_arms_then_enters_only_after_pullback_reclaim(self):
        strategy = MicroReversalProbeStrategy(SETTINGS)
        armed = strategy.observe(snapshot())
        self.assertEqual(armed.type, "MICRO_ARMED")
        pullback = strategy.observe(snapshot(
            timestamp_ms=1_060_000, price=99.9,
            micro_open=100.0, micro_high=100.05, micro_low=99.75, micro_close=99.85,
            micro_open_time_ms=1_060_000,
        ))
        self.assertEqual(pullback.type, "MICRO_PULLBACK")
        signal = strategy.observe(snapshot(
            timestamp_ms=1_120_000, price=100.1,
            micro_open=99.85, micro_high=100.15, micro_low=99.8, micro_close=100.1,
            micro_previous_high=99.95, micro_open_time_ms=1_120_000,
        ))
        self.assertEqual(signal.type, "SIGNAL")
        self.assertEqual(signal.payload["setup"], "MICRO_REVERSAL_PROBE")
        self.assertLess(signal.payload["stop_price"], signal.payload["entry_price"])

    def test_late_directional_move_is_rejected(self):
        event = MicroReversalProbeStrategy(SETTINGS).observe(
            snapshot(price_change_15m_pct=1.10)
        )
        self.assertIsNone(event)

    def test_oi_and_taker_must_support_the_turn(self):
        event = MicroReversalProbeStrategy(SETTINGS).observe(snapshot(
            oi_change_5m_pct=-0.10,
            oi_acceleration_5m_pct_points=-0.10,
            taker_buy_sell_ratio=0.90,
        ))
        self.assertIsNone(event)

    def test_structure_break_invalidates_armed_turn(self):
        strategy = MicroReversalProbeStrategy(SETTINGS)
        strategy.observe(snapshot())
        event = strategy.observe(snapshot(
            timestamp_ms=1_060_000, micro_open_time_ms=1_060_000,
            micro_open=99.7, micro_high=99.8, micro_low=99.3, micro_close=99.35,
        ))
        self.assertEqual(event.type, "INVALIDATED")


if __name__ == "__main__":
    unittest.main()
