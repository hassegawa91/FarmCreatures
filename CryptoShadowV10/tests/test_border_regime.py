import unittest

from engine.binance import MarketSnapshot
from engine.border_regime import BorderRegimeStrategy, BorderState


def market(**overrides):
    values = dict(
        symbol="TESTUSDT", timestamp_ms=1_000_000, price=101.1,
        price_change_15m_pct=1.0, oi_change_15m_pct=0.3, global_lsr=1.0,
        taker_buy_sell_ratio=1.1, funding_rate_pct=0.0, volume_ratio=1.5,
        spread_pct=0.02, prior_high=101.0, prior_low=99.0,
        candle_high=101.3, candle_low=100.8, candle_open=100.9, candle_close=101.1,
        candle_open_time_ms=900_000, previous_candle_close=100.9,
        atr14=0.5, atr14_pct=0.5, adx14=20.0, price_change_5m_pct=0.2,
        oi_change_5m_pct=0.1, oi_acceleration_5m_pct_points=0.05,
        edge_range_high=101.0, edge_range_low=99.0, edge_range_width_pct=2.0,
    )
    values.update(overrides)
    return MarketSnapshot(**values)


class BorderRegimeTests(unittest.TestCase):
    SETTINGS = {
        "border_breakout_min_volume_ratio": 1.25,
        "border_breakout_min_oi_15m_pct": 0.10,
        "border_breakout_min_oi_acceleration": 0.0,
        "border_breakout_long_min_taker": 1.03,
        "border_breakout_short_max_taker": 0.97,
        "border_breakout_min_stop_pct": 0.65,
        "border_breakout_max_stop_pct": 1.20,
        "max_spread_pct": 0.08,
    }

    def test_breakout_requires_fresh_oi_acceleration(self):
        strategy = BorderRegimeStrategy(self.SETTINGS)
        checks = strategy._breakout_checks(
            market(oi_acceleration_5m_pct_points=-0.01), "LONG",
        )
        self.assertFalse(checks["oi_accelerates"])
        self.assertFalse(all(checks.values()))

    def test_breakout_stop_uses_retested_structure_within_cap(self):
        strategy = BorderRegimeStrategy(self.SETTINGS)
        state = BorderState(
            symbol="TESTUSDT", armed_at_ms=1, expires_at_ms=2_000_000,
            last_candle_ms=900_000, range_low=99.0, range_high=101.0,
            breakout_direction="LONG", breakout_boundary=101.0,
        )
        stop_pct = strategy._breakout_stop_pct(
            state, market(price=101.1, candle_low=100.8), "LONG",
        )
        self.assertAlmostEqual(stop_pct, 0.65)
        self.assertIsNone(strategy._breakout_stop_pct(
            state, market(price=103.0, candle_low=101.0), "LONG",
        ))


if __name__ == "__main__":
    unittest.main()
