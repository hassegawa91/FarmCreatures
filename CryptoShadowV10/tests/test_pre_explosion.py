import unittest

from engine.binance import MarketSnapshot
from engine.pre_explosion import PreExplosionAnticipationStrategy, PreExplosionReversalStrategy


SETTINGS = {
    "long_max_global_lsr": 2.2, "short_min_global_lsr": 0.45,
    "long_max_funding_pct": 0.05, "short_min_funding_pct": -0.05,
    "max_spread_pct": 0.08, "long_max_taker_ratio": 1.8,
    "short_min_taker_ratio": 0.55,
    "pre_explosion_max_compression_ratio": 0.8,
    "pre_explosion_max_range_width_pct": 1.5,
    "pre_explosion_near_boundary_pct": 0.25,
    "pre_explosion_max_trigger_extension_pct": 0.30,
    "pre_explosion_max_prearm_extension_pct": 0.08,
    "pre_explosion_min_pre_volume_ratio": 0.8,
    "pre_explosion_min_oi_change_pct": 0.1,
    "pre_explosion_min_oi_acceleration_pct_points": 0.0,
    "pre_explosion_oi_hold_tolerance_pct_points": 0.05,
    "pre_explosion_absorption_oi_acceleration_pct_points": 0.08,
    "pre_explosion_min_price_acceleration_pct": 0.05,
    "pre_explosion_min_directional_impulse_pct": 0.0,
    "pre_explosion_min_price_oi_efficiency": 0.5,
    "pre_explosion_max_trigger_compression_ratio": 1.2,
    "pre_explosion_min_trigger_volume_ratio": 1.0,
    "pre_explosion_trigger_buffer_pct": 0.03,
    "pre_explosion_stop_behind_boundary_pct": 0.35,
    "pre_explosion_max_stop_pct": 0.65,
    "pre_explosion_target_r": 2.5,
}


def market(price=100.0, ts=1_000_000, direction="LONG", **changes):
    long = direction == "LONG"
    row = dict(
        symbol="TESTUSDT", timestamp_ms=ts, price=price,
        price_change_15m_pct=0.1 if long else -0.1,
        oi_change_15m_pct=0.2, global_lsr=1.1,
        taker_buy_sell_ratio=1.1 if long else 0.9,
        funding_rate_pct=0.005, volume_ratio=1.0, spread_pct=0.02,
        prior_high=100.1 if long else 101.0,
        prior_low=99.0 if long else 99.9,
        candle_high=price, candle_low=price,
        range_width_pct=1.0, compression_ratio=0.6,
        distance_to_prior_high_pct=0.1 if long else 1.0,
        distance_to_prior_low_pct=1.0 if long else 0.1,
    )
    row.update(changes)
    return MarketSnapshot(**row)


class PreExplosionTests(unittest.TestCase):
    def test_anticipation_enters_during_compression_before_breakout(self):
        strategy = PreExplosionAnticipationStrategy(SETTINGS)
        event = strategy.observe(market(candle_low=99.8, candle_high=100.05))
        self.assertEqual(event.type, "SIGNAL")
        self.assertEqual(event.payload["setup"], "PRE_EXPLOSION_ANTICIPATION")
        self.assertLess(event.payload["stop_price"], event.payload["entry_price"])

    def test_anticipation_has_per_symbol_cooldown(self):
        strategy = PreExplosionAnticipationStrategy({
            **SETTINGS, "anticipation_cooldown_minutes": 15,
        })
        self.assertEqual(strategy.observe(market()).type, "SIGNAL")
        self.assertIsNone(strategy.observe(market(ts=1_060_000)))

    def test_xrp_like_weak_oi_does_not_prearm(self):
        settings = {**SETTINGS, "pre_explosion_min_oi_change_pct": 0.15}
        strategy = PreExplosionReversalStrategy(settings)
        self.assertIsNone(strategy.observe(market(
            direction="SHORT", oi_change_15m_pct=0.11,
            volume_ratio=1.05, taker_buy_sell_ratio=0.93,
        )))

    def test_long_prearms_then_triggers_on_price_oi_and_flow(self):
        strategy = PreExplosionReversalStrategy(SETTINGS)
        event = strategy.observe(market())
        self.assertEqual(event.type, "PRE_ARMED")
        event = strategy.observe(market(
            price=100.17, ts=1_060_000, oi_change_15m_pct=0.24,
            volume_ratio=1.2, taker_buy_sell_ratio=1.2,
        ))
        self.assertEqual(event.type, "SIGNAL")
        self.assertEqual(event.payload["setup"], "PRE_EXPLOSION_REVERSAL")
        risk = event.payload["entry_price"] - event.payload["stop_price"]
        self.assertAlmostEqual(event.payload["target_price"] - event.payload["entry_price"], 2.5 * risk)

    def test_short_is_symmetric(self):
        strategy = PreExplosionReversalStrategy(SETTINGS)
        self.assertEqual(strategy.observe(market(direction="SHORT")).type, "PRE_ARMED")
        event = strategy.observe(market(
            price=99.83, ts=1_060_000, direction="SHORT", oi_change_15m_pct=0.24,
            volume_ratio=1.2, taker_buy_sell_ratio=0.8,
        ))
        self.assertEqual(event.type, "SIGNAL")
        self.assertEqual(event.direction, "SHORT")

    def test_oi_may_hold_between_five_minute_updates(self):
        strategy = PreExplosionReversalStrategy(SETTINGS)
        strategy.observe(market())
        event = strategy.observe(market(
            price=100.17, ts=1_060_000, oi_change_15m_pct=0.2,
            volume_ratio=1.2, taker_buy_sell_ratio=1.2,
        ))
        self.assertEqual(event.type, "SIGNAL")

    def test_taker_climax_does_not_prearm(self):
        strategy = PreExplosionReversalStrategy(SETTINGS)
        self.assertIsNone(strategy.observe(market(taker_buy_sell_ratio=2.2)))

    def test_does_not_prearm_after_breakout_is_already_extended(self):
        strategy = PreExplosionReversalStrategy(SETTINGS)
        self.assertIsNone(strategy.observe(market(distance_to_prior_high_pct=-0.27)))

    def test_rising_oi_without_price_response_is_absorption_not_trigger(self):
        strategy = PreExplosionReversalStrategy(SETTINGS)
        strategy.observe(market())
        event = strategy.observe(market(
            price=100.02, ts=1_060_000, oi_change_15m_pct=0.40,
            volume_ratio=1.4, taker_buy_sell_ratio=1.3,
        ))
        self.assertIsNone(event)
        checks = strategy.diagnose(market(
            price=100.02, ts=1_060_000, oi_change_15m_pct=0.40,
            volume_ratio=1.4, taker_buy_sell_ratio=1.3,
        ))["checks"]
        self.assertFalse(checks["price_accelerates"])
        self.assertFalse(checks["price_oi_efficiency"])

    def test_opposite_impulse_blocks_long_trigger(self):
        strategy = PreExplosionReversalStrategy(SETTINGS)
        strategy.observe(market())
        self.assertIsNone(strategy.observe(market(
            price=100.17, ts=1_060_000, oi_change_15m_pct=0.24,
            volume_ratio=1.2, taker_buy_sell_ratio=1.2, price_change_15m_pct=-0.14,
        )))

    def test_prearm_boundary_is_frozen_and_does_not_chase_price(self):
        strategy = PreExplosionReversalStrategy(SETTINGS)
        strategy.observe(market())
        event = strategy.observe(market(
            price=100.17, ts=1_060_000, oi_change_15m_pct=0.24,
            volume_ratio=1.2, taker_buy_sell_ratio=1.2, prior_high=100.16,
        ))
        self.assertEqual(event.type, "SIGNAL")

    def test_broken_compression_blocks_trigger(self):
        strategy = PreExplosionReversalStrategy(SETTINGS)
        strategy.observe(market())
        self.assertIsNone(strategy.observe(market(
            price=100.17, ts=1_060_000, oi_change_15m_pct=0.24,
            volume_ratio=1.2, taker_buy_sell_ratio=1.2, compression_ratio=1.21,
        )))

    def test_move_away_invalidates_candidate(self):
        strategy = PreExplosionReversalStrategy(SETTINGS)
        strategy.observe(market())
        event = strategy.observe(market(price=99.6, ts=1_060_000))
        self.assertEqual(event.type, "INVALIDATED")
        self.assertEqual(event.reason, "moved_away_from_boundary")


if __name__ == "__main__":
    unittest.main()
