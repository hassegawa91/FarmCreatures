import unittest

from engine.binance import MarketSnapshot
from engine.momentum_staircase import MomentumEarlyStrategy, MomentumStaircaseStrategy


SETTINGS = {
    "long_max_funding_pct": 0.05, "short_min_funding_pct": -0.05,
    "max_spread_pct": 0.08,
    "momentum_min_directional_candles": 2,
    "momentum_min_impulse_pct": 1.2, "momentum_max_arm_impulse_pct": 3.0,
    "momentum_early_max_impulse_pct": 1.8,
    "momentum_min_oi_change_pct": 0.05,
    "momentum_min_oi_acceleration_pct_points": 0.08,
    "momentum_min_volume_ratio": 0.75,
    "momentum_min_volume_growth_ratio": 1.15,
    "momentum_long_taker_min": 1.15, "momentum_long_taker_max": 1.8,
    "momentum_short_taker_min": 0.55, "momentum_short_taker_max": 0.87,
    "momentum_max_lsr_deterioration_pct": 0.75,
    "momentum_min_pullback_pct": 0.10, "momentum_max_pullback_pct": 0.60,
    "momentum_invalidation_pullback_pct": 0.80,
    "momentum_min_reclaim_pct": 0.08,
    "momentum_max_entry_extension_pct": 1.5,
    "momentum_entry_min_oi_change_pct": 0.02,
    "momentum_entry_min_volume_ratio": 0.8,
    "momentum_entry_long_taker_min": 1.05,
    "momentum_entry_short_taker_max": 0.95,
    "momentum_entry_max_lsr_deterioration_pct": 2.0,
    "momentum_stop_buffer_pct": 0.08,
    "momentum_min_stop_pct": 0.3, "momentum_max_stop_pct": 0.8,
    "momentum_target_r": 2.2,
    "momentum_early_stop_buffer_pct": 0.08,
    "momentum_early_min_stop_pct": 0.3,
    "momentum_early_max_stop_pct": 0.8,
    "momentum_early_target_r": 2.2,
}


def market(price=100.0, ts=1_000_000, direction="LONG", **changes):
    long = direction == "LONG"
    row = dict(
        symbol="LABUSDT", timestamp_ms=ts, price=price,
        price_change_15m_pct=2.39 if long else -2.39,
        oi_change_15m_pct=0.088, oi_change_5m_pct=0.05,
        oi_acceleration_5m_pct_points=0.136,
        global_lsr=3.90 if long else 0.30,
        lsr_change_5m_pct=-0.06 if long else 0.06,
        taker_buy_sell_ratio=1.585 if long else 0.70,
        funding_rate_pct=0.01, volume_ratio=0.90,
        volume_growth_ratio=1.80, directional_candle_count=3,
        spread_pct=0.02,
        prior_high=99.0 if long else 101.0, prior_low=99.0 if long else 101.0,
        candle_high=100.1, candle_low=99.5,
        micro_open=99.8 if long else 100.2,
        micro_high=100.1, micro_low=99.6, micro_close=100.0,
        micro_previous_high=99.9, micro_previous_low=100.1,
        micro_open_time_ms=ts,
    )
    row.update(changes)
    return MarketSnapshot(**row)


class MomentumStaircaseTests(unittest.TestCase):
    def test_lab_like_acceleration_arms_despite_high_but_stable_lsr(self):
        strategy = MomentumStaircaseStrategy(SETTINGS)
        event = strategy.observe(market())
        self.assertEqual(event.type, "MOMENTUM_ARMED")
        self.assertEqual(event.direction, "LONG")

    def test_pullback_then_one_minute_reclaim_signals(self):
        strategy = MomentumStaircaseStrategy(SETTINGS)
        strategy.observe(market())
        pullback = strategy.observe(market(
            price=100.1, ts=1_060_000,
            micro_open=100.4, micro_high=100.5, micro_low=99.9, micro_close=100.1,
            micro_open_time_ms=1_060_000,
        ))
        self.assertEqual(pullback.type, "MOMENTUM_PULLBACK")
        signal = strategy.observe(market(
            price=100.35, ts=1_120_000, oi_change_15m_pct=0.10,
            volume_ratio=1.0, taker_buy_sell_ratio=1.30,
            micro_open=100.05, micro_high=100.4, micro_low=100.0, micro_close=100.35,
            micro_previous_high=100.2, micro_open_time_ms=1_120_000,
        ))
        self.assertEqual(signal.type, "SIGNAL")
        self.assertEqual(signal.payload["setup"], "OI_MOMENTUM_PULLBACK")

    def test_early_profile_enters_with_microstructure_stop(self):
        strategy = MomentumEarlyStrategy(SETTINGS)
        signal = strategy.observe(market(price_change_15m_pct=1.60))
        self.assertEqual(signal.type, "SIGNAL")
        self.assertEqual(signal.payload["setup"], "OI_MOMENTUM_EARLY")
        self.assertLess(signal.payload["stop_price"], signal.payload["entry_price"])

    def test_early_profile_does_not_chase_an_already_extended_impulse(self):
        strategy = MomentumEarlyStrategy(SETTINGS)
        self.assertIsNone(strategy.observe(market(price_change_15m_pct=1.81)))

    def test_late_extended_pump_does_not_arm(self):
        strategy = MomentumStaircaseStrategy(SETTINGS)
        self.assertIsNone(strategy.observe(market(price_change_15m_pct=3.44)))

    def test_oi_without_acceleration_does_not_arm(self):
        strategy = MomentumStaircaseStrategy(SETTINGS)
        self.assertIsNone(strategy.observe(market(oi_acceleration_5m_pct_points=0.02)))


if __name__ == "__main__":
    unittest.main()
