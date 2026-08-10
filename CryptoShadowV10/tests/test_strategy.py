import unittest

from engine.binance import MarketSnapshot
from engine.strategy import OIBreakoutRetestStrategy, OIExpansionArmProbeStrategy


SETTINGS = {
    "min_price_impulse_pct": 0.7, "min_oi_change_15m_pct": 0.25,
    "min_volume_ratio": 1.4, "min_entry_volume_ratio": 1.2,
    "long_min_taker_ratio": 1.05, "long_max_taker_ratio": 1.8,
    "short_min_taker_ratio": 0.55, "short_max_taker_ratio": 0.95,
    "long_max_global_lsr": 2.2, "short_min_global_lsr": 0.45,
    "long_max_funding_pct": 0.05, "short_min_funding_pct": -0.05,
    "max_spread_pct": 0.08, "confirmation_min_minutes": 5,
    "min_price_extension_pct": 0.12, "min_oi_acceleration_pct_points": 0.05,
    "oi_hold_tolerance_pct_points": 0.08, "retest_touch_pct": 0.12,
    "retest_reclaim_pct": 0.08,
    "retest_min_minutes": 1, "retest_allow_held_oi_sample": True,
    "retest_max_oi_sample_age_minutes": 6,
    "max_lsr_deterioration_pct": 3.0, "max_entry_extension_from_boundary_pct": 0.55,
    "max_adverse_move_pct": 0.25, "setup_ttl_minutes": 20, "cooldown_minutes": 90,
    "min_stop_pct": 0.3, "max_stop_pct": 0.65, "stop_buffer_pct": 0.05,
    "target_r": 2.2,
}


def market(price=100.0, ts=1_000_000, direction="LONG", **changes):
    sign = 1 if direction == "LONG" else -1
    row = dict(
        symbol="TESTUSDT", timestamp_ms=ts, price=price,
        price_change_15m_pct=0.8 * sign,
        oi_change_15m_pct=0.35, global_lsr=1.1,
        taker_buy_sell_ratio=1.15 if sign > 0 else 0.85,
        funding_rate_pct=0.01, volume_ratio=1.5, spread_pct=0.02,
        prior_high=99.8 if sign > 0 else 101.0,
        prior_low=99.0 if sign > 0 else 100.2,
        candle_high=price, candle_low=price,
        oi_timestamp_ms=ts, lsr_timestamp_ms=ts, taker_timestamp_ms=ts,
    )
    row.update(changes)
    return MarketSnapshot(**row)


class StrategyTests(unittest.TestCase):
    def test_arm_probe_enters_on_first_complete_expansion_without_waiting_for_retest(self):
        settings = {
            **SETTINGS, "arm_probe_max_impulse_pct": 1.8,
            "arm_probe_min_oi_acceleration_pct_points": 0.03,
            "arm_probe_min_volume_growth_ratio": 1.0,
        }
        strategy = OIExpansionArmProbeStrategy(settings)
        event = strategy.observe(market(
            oi_acceleration_5m_pct_points=0.10, volume_growth_ratio=1.30,
            candle_low=99.7, candle_high=100.1,
        ))
        self.assertEqual(event.type, "SIGNAL")
        self.assertEqual(event.payload["setup"], "OI_EXPANSION_ARM_PROBE")

    def test_arm_probe_rejects_late_or_nonaccelerating_expansion(self):
        strategy = OIExpansionArmProbeStrategy(SETTINGS)
        self.assertIsNone(strategy.observe(market(
            price_change_15m_pct=1.81, oi_acceleration_5m_pct_points=0.10,
            volume_growth_ratio=1.30,
        )))
        self.assertIsNone(strategy.observe(market(
            oi_acceleration_5m_pct_points=0.0, volume_growth_ratio=1.30,
        )))

    def test_long_waits_for_fresh_price_and_oi_acceleration(self):
        strategy = OIBreakoutRetestStrategy(SETTINGS)
        self.assertEqual(strategy.observe(market()).type, "ARMED")
        self.assertIsNone(strategy.observe(market(price=100.2, ts=1_240_000, oi_change_15m_pct=0.45)))
        event = strategy.observe(market(price=100.2, ts=1_300_000, oi_change_15m_pct=0.45))
        self.assertEqual(event.type, "SIGNAL")
        self.assertEqual(event.payload["setup"], "OI_EXPANSION_CONFIRMATION")
        self.assertAlmostEqual(event.payload["target_price"] - event.payload["entry_price"],
                               2.2 * (event.payload["entry_price"] - event.payload["stop_price"]))

    def test_short_is_symmetric(self):
        strategy = OIBreakoutRetestStrategy(SETTINGS)
        self.assertEqual(strategy.observe(market(direction="SHORT")).type, "ARMED")
        event = strategy.observe(market(
            price=99.8, ts=1_300_000, direction="SHORT", oi_change_15m_pct=0.45,
        ))
        self.assertEqual(event.type, "SIGNAL")
        self.assertEqual(event.direction, "SHORT")

    def test_oi_contraction_never_arms(self):
        strategy = OIBreakoutRetestStrategy(SETTINGS)
        self.assertIsNone(strategy.observe(market(oi_change_15m_pct=-0.5)))

    def test_crowded_long_never_arms(self):
        strategy = OIBreakoutRetestStrategy(SETTINGS)
        self.assertIsNone(strategy.observe(market(global_lsr=2.5)))

    def test_confirmation_requires_fresh_oi_sample_but_not_extra_acceleration(self):
        strategy = OIBreakoutRetestStrategy(SETTINGS)
        strategy.observe(market())
        self.assertIsNone(strategy.observe(market(
            price=100.2, ts=1_300_000, oi_change_15m_pct=0.35, oi_timestamp_ms=1_000_000,
        )))
        event = strategy.observe(market(price=100.2, ts=1_360_000, oi_change_15m_pct=0.35))
        self.assertEqual(event.type, "SIGNAL")

    def test_retest_then_reclaim_can_confirm_expansion(self):
        strategy = OIBreakoutRetestStrategy(SETTINGS)
        strategy.observe(market())
        pullback = strategy.observe(market(
            price=99.9, ts=1_300_000, oi_change_15m_pct=0.34,
        ))
        self.assertEqual(pullback.type, "PULLBACK")
        event = strategy.observe(market(
            price=100.02, ts=1_360_000, oi_change_15m_pct=0.34,
        ))
        self.assertEqual(event.type, "SIGNAL")

    def test_retest_reclaim_can_enter_before_fixed_five_minute_window(self):
        strategy = OIBreakoutRetestStrategy(SETTINGS)
        strategy.observe(market(micro_open_time_ms=1_000_000))
        pullback = strategy.observe(market(
            price=99.9, ts=1_060_000, oi_timestamp_ms=1_000_000,
            micro_open_time_ms=1_060_000, micro_low=99.88,
        ))
        self.assertEqual(pullback.type, "PULLBACK")
        event = strategy.observe(market(
            price=100.02, ts=1_120_000, oi_timestamp_ms=1_000_000,
            micro_open_time_ms=1_120_000, micro_low=99.95,
        ))
        self.assertEqual(event.type, "SIGNAL")
        self.assertEqual(event.reason, "breakout_retest_reclaim")
        self.assertEqual(event.payload["evidence"]["entry_model"], "RETEST_RECLAIM")

    def test_retest_does_not_reenter_on_same_micro_candle(self):
        strategy = OIBreakoutRetestStrategy(SETTINGS)
        strategy.observe(market(micro_open_time_ms=1_000_000))
        strategy.observe(market(
            price=99.9, ts=1_060_000, oi_timestamp_ms=1_000_000,
            micro_open_time_ms=1_060_000, micro_low=99.88,
        ))
        self.assertIsNone(strategy.observe(market(
            price=100.02, ts=1_090_000, oi_timestamp_ms=1_000_000,
            micro_open_time_ms=1_060_000, micro_low=99.95,
        )))

    def test_taker_climax_blocks_arm_and_confirmation(self):
        arm_climax = OIBreakoutRetestStrategy(SETTINGS)
        self.assertIsNone(arm_climax.observe(market(taker_buy_sell_ratio=2.4)))

        confirmation_climax = OIBreakoutRetestStrategy(SETTINGS)
        confirmation_climax.observe(market())
        self.assertIsNone(confirmation_climax.observe(market(
            price=100.2, ts=1_300_000, oi_change_15m_pct=0.45, taker_buy_sell_ratio=2.4,
        )))

    def test_failed_expansion_invalidates_instead_of_waiting_for_retest(self):
        strategy = OIBreakoutRetestStrategy(SETTINGS)
        strategy.observe(market())
        event = strategy.observe(market(price=99.7, ts=1_060_000))
        self.assertEqual(event.type, "INVALIDATED")
        self.assertEqual(event.reason, "expansion_failed")

    def test_overextended_entry_is_not_chased(self):
        strategy = OIBreakoutRetestStrategy(SETTINGS)
        strategy.observe(market())
        self.assertIsNone(strategy.observe(market(
            price=100.5, ts=1_300_000, oi_change_15m_pct=0.45,
        )))


if __name__ == "__main__":
    unittest.main()
