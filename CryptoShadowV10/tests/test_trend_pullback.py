import unittest

from engine.binance import MarketSnapshot
from engine.trend_pullback import FlowTrendPullbackStrategy


SETTINGS = {"max_spread_pct": 0.08}


def market(ts=1_000_000, **changes):
    row = dict(
        symbol="TESTUSDT", timestamp_ms=ts, price=101.0,
        price_change_15m_pct=0.80, oi_change_15m_pct=0.20,
        global_lsr=1.1, taker_buy_sell_ratio=1.10, funding_rate_pct=0.01,
        volume_ratio=1.0, spread_pct=0.02, prior_high=101.2, prior_low=99.5,
        candle_high=101.2, candle_low=100.7, candle_open=100.8, candle_close=101.0,
        candle_open_time_ms=ts, previous_candle_close=100.7,
        previous_candle_high=100.9, previous_candle_low=100.5,
        ema21=100.5, ema21_slope_pct=0.03, atr14=0.5, atr14_pct=0.5, adx14=28.0,
        lsr_change_5m_pct=0.0,
    )
    row.update(changes)
    return MarketSnapshot(**row)


class TrendPullbackTests(unittest.TestCase):
    def test_long_waits_for_pullback_then_fresh_reclaim(self):
        strategy = FlowTrendPullbackStrategy(SETTINGS)
        armed = strategy.observe(market())
        self.assertEqual(armed.type, "TREND_ARMED")
        pullback = strategy.observe(market(
            ts=1_300_000, candle_open_time_ms=1_300_000, price=100.7,
            candle_open=101.1, candle_close=100.7, candle_high=101.3, candle_low=100.3,
            previous_candle_high=101.2, previous_candle_low=100.7,
        ))
        self.assertEqual(pullback.type, "TREND_PULLBACK")
        signal = strategy.observe(market(
            ts=1_600_000, candle_open_time_ms=1_600_000, price=101.1,
            candle_open=100.7, candle_close=101.1, candle_high=101.2, candle_low=100.6,
            previous_candle_high=100.9, previous_candle_low=100.3,
        ))
        self.assertEqual(signal.type, "SIGNAL")
        self.assertEqual(signal.payload["setup"], "FLOW_TREND_PULLBACK")
        self.assertEqual(signal.direction, "LONG")

    def test_does_not_react_twice_to_same_closed_candle(self):
        strategy = FlowTrendPullbackStrategy(SETTINGS)
        strategy.observe(market())
        self.assertIsNone(strategy.observe(market(ts=1_060_000, candle_open_time_ms=1_000_000)))
        self.assertEqual(strategy.snapshot()["TESTUSDT"]["phase"], "TREND_ARMED")


if __name__ == "__main__":
    unittest.main()
