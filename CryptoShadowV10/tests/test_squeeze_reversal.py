import unittest

from engine.binance import MarketSnapshot
from engine.squeeze_reversal import PostSqueezeReversalStrategy


SETTINGS = {
    "max_spread_pct": 0.08,
    "squeeze_min_price_impulse_pct": 1.0,
    "squeeze_min_oi_change_pct": 0.25,
    "squeeze_min_volume_ratio": 1.6,
    "squeeze_up_taker_climax": 1.55,
    "squeeze_down_taker_climax": 0.68,
    "squeeze_up_min_lsr": 1.45,
    "squeeze_down_max_lsr": 0.8,
    "squeeze_confirmation_minutes": 5,
    "squeeze_min_reversal_pct": 0.25,
    "squeeze_min_retracement_fraction": 0.25,
    "squeeze_max_oi_change_5m_pct": 0.05,
    "squeeze_short_max_close_location": 0.45,
    "squeeze_long_min_close_location": 0.55,
    "squeeze_short_confirm_taker": 0.95,
    "squeeze_long_confirm_taker": 1.05,
    "squeeze_min_confirmation_volume_ratio": 1.0,
    "squeeze_stop_buffer_pct": 0.15,
    "squeeze_max_stop_pct": 0.9,
    "squeeze_target_r": 2.2,
}


def market(price=101.2, ts=1_000_000, direction="UP", **changes):
    up = direction == "UP"
    row = dict(
        symbol="TESTUSDT", timestamp_ms=ts, price=price,
        price_change_15m_pct=1.4 if up else -1.4,
        oi_change_15m_pct=0.45, global_lsr=1.7 if up else 0.7,
        taker_buy_sell_ratio=1.75 if up else 0.58,
        funding_rate_pct=0.01, volume_ratio=2.2, spread_pct=0.02,
        prior_high=101.0 if up else 100.0, prior_low=100.0 if up else 99.0,
        candle_high=price, candle_low=price,
        oi_timestamp_ms=ts, lsr_timestamp_ms=ts, taker_timestamp_ms=ts,
        oi_change_5m_pct=0.02,
        candle_open=100.8 if up else 99.2,
        candle_close=price,
        candle_open_time_ms=ts,
        previous_candle_close=100.8 if up else 99.2,
        previous_candle_high=101.0 if up else 99.4,
        previous_candle_low=100.6 if up else 99.0,
    )
    row.update(changes)
    return MarketSnapshot(**row)


class PostSqueezeReversalTests(unittest.TestCase):
    def test_up_squeeze_requires_exhaustion_then_signals_short(self):
        strategy = PostSqueezeReversalStrategy(SETTINGS)
        armed = strategy.observe(market())
        self.assertEqual(armed.type, "SQUEEZE_ARMED")
        self.assertEqual(armed.direction, "SHORT")
        event = strategy.observe(market(
            price=100.85, ts=1_300_000, oi_change_15m_pct=0.43,
            taker_buy_sell_ratio=0.90, volume_ratio=1.4,
            candle_open=101.15, candle_close=100.85, candle_high=101.20, candle_low=100.75,
            previous_candle_close=101.10,
        ))
        self.assertEqual(event.type, "SIGNAL")
        self.assertEqual(event.payload["setup"], "POST_SQUEEZE_REVERSAL")
        self.assertGreater(event.payload["stop_price"], event.payload["entry_price"])
        self.assertLess(event.payload["target_price"], event.payload["entry_price"])

    def test_down_squeeze_is_symmetric(self):
        strategy = PostSqueezeReversalStrategy(SETTINGS)
        self.assertEqual(strategy.observe(market(price=98.8, direction="DOWN")).direction, "LONG")
        event = strategy.observe(market(
            price=99.15, ts=1_300_000, direction="DOWN", oi_change_15m_pct=0.43,
            taker_buy_sell_ratio=1.10, volume_ratio=1.4,
            candle_open=98.85, candle_close=99.15, candle_high=99.25, candle_low=98.80,
            previous_candle_close=98.90,
        ))
        self.assertEqual(event.type, "SIGNAL")
        self.assertEqual(event.direction, "LONG")

    def test_continuing_oi_and_one_sided_taker_do_not_reverse(self):
        strategy = PostSqueezeReversalStrategy(SETTINGS)
        strategy.observe(market())
        self.assertIsNone(strategy.observe(market(
            price=100.85, ts=1_300_000, oi_change_15m_pct=0.60,
            taker_buy_sell_ratio=1.40, volume_ratio=1.4, oi_change_5m_pct=0.20,
        )))

    def test_lab_like_seller_flow_absorbed_by_rising_price_is_blocked(self):
        strategy = PostSqueezeReversalStrategy(SETTINGS)
        strategy.observe(market(
            price=0.15656, price_change_15m_pct=3.44,
            oi_change_15m_pct=0.334, global_lsr=3.87,
            taker_buy_sell_ratio=1.585, volume_ratio=1.94,
            prior_high=0.152, prior_low=0.1487,
            candle_open=0.1544, candle_close=0.1565,
            candle_high=0.1567, candle_low=0.1543,
            previous_candle_close=0.1544,
        ))
        event = strategy.observe(market(
            price=0.157, ts=1_300_000, price_change_15m_pct=3.28,
            oi_change_15m_pct=0.257, oi_change_5m_pct=-0.0012,
            global_lsr=3.87, taker_buy_sell_ratio=0.666,
            volume_ratio=2.82, prior_high=0.1528, prior_low=0.1487,
            candle_open=0.1565, candle_close=0.1574,
            candle_high=0.1575, candle_low=0.1556,
            previous_candle_close=0.1565,
        ))
        self.assertIsNone(event)
        checks = strategy.diagnose(market(
            price=0.157, ts=1_300_000, price_change_15m_pct=3.28,
            oi_change_15m_pct=0.257, oi_change_5m_pct=-0.0012,
            global_lsr=3.87, taker_buy_sell_ratio=0.666,
            volume_ratio=2.82, prior_high=0.1528, prior_low=0.1487,
            candle_open=0.1565, candle_close=0.1574,
            candle_high=0.1575, candle_low=0.1556,
            previous_candle_close=0.1565,
        ))["checks"]
        self.assertFalse(checks["structure_reverses"])
        self.assertFalse(checks["price_flow_confirms"])

    def test_ordinary_move_does_not_arm(self):
        strategy = PostSqueezeReversalStrategy(SETTINGS)
        self.assertIsNone(strategy.observe(market(price_change_15m_pct=0.7)))


if __name__ == "__main__":
    unittest.main()
