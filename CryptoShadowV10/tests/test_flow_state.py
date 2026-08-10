import unittest

from engine.binance import MarketSnapshot
from engine.flow_state import FlowLongCampaignStrategy, FlowStructuralReversalStrategy


SETTINGS = {
    "max_spread_pct": 0.08,
    "long_campaign_max_compression_ratio": 0.95,
    "long_campaign_max_range_width_pct": 1.8,
    "long_campaign_near_boundary_pct": 0.40,
    "long_campaign_max_prearm_extension_pct": 0.05,
    "long_campaign_min_arm_oi_15m_pct": 0.05,
    "long_campaign_max_arm_lsr_slope_pct": 0.10,
    "long_campaign_min_arm_volume_ratio": 0.65,
    "long_campaign_max_arm_taker": 1.8,
    "long_campaign_max_funding_pct": 0.05,
    "long_campaign_max_stop_pct": 1.4,
    "structural_reversal_min_price_impulse_pct": 1.0,
    "structural_reversal_min_arm_oi_15m_pct": 0.2,
    "structural_reversal_min_arm_volume_ratio": 1.4,
    "structural_reversal_min_age_minutes": 5,
    "structural_reversal_min_retracement_pct": 0.3,
    "structural_reversal_min_oi_deterioration_points": 0.15,
    "structural_reversal_max_oi_5m_pct": 0.0,
    "structural_reversal_min_confirm_volume_ratio": 0.8,
    "structural_reversal_max_stop_pct": 1.5,
}


def market(price=100.8, ts=1_000_000, **changes):
    row = dict(
        symbol="TESTUSDT", timestamp_ms=ts, price=price,
        price_change_15m_pct=0.20, oi_change_15m_pct=0.20,
        global_lsr=1.10, taker_buy_sell_ratio=1.10,
        funding_rate_pct=0.005, volume_ratio=0.90, spread_pct=0.02,
        prior_high=101.0, prior_low=99.8, candle_high=100.9, candle_low=100.5,
        range_width_pct=1.0, compression_ratio=0.70,
        distance_to_prior_high_pct=(101.0 - price) / price * 100.0,
        distance_to_prior_low_pct=(price - 99.8) / price * 100.0,
        oi_timestamp_ms=ts, lsr_timestamp_ms=ts, taker_timestamp_ms=ts,
        oi_change_5m_pct=0.05, candle_open=100.6, candle_close=100.8,
        candle_open_time_ms=ts, previous_candle_close=100.6,
        previous_candle_high=100.8, previous_candle_low=100.4,
        oi_acceleration_5m_pct_points=0.02, lsr_change_5m_pct=-0.20,
    )
    row.update(changes)
    return MarketSnapshot(**row)


class FlowStateTests(unittest.TestCase):
    def test_operational_engine_uses_new_explicit_strategy_names(self):
        self.assertEqual(FlowLongCampaignStrategy(SETTINGS).name, "FLOW_LONG_CAMPAIGN")
        self.assertEqual(FlowStructuralReversalStrategy(SETTINGS).name, "FLOW_STRUCTURAL_REVERSAL")

    def test_long_campaign_records_arm_breakout_and_confirmation(self):
        strategy = FlowLongCampaignStrategy(SETTINGS)
        armed = strategy.observe(market())
        self.assertEqual(armed.type, "CAMPAIGN_ARMED")
        breakout = strategy.observe(market(
            price=101.10, ts=1_300_000, candle_open_time_ms=1_300_000,
            candle_open=100.85, candle_close=101.10, candle_high=101.20, candle_low=100.80,
            previous_candle_close=100.80, previous_candle_high=100.90, previous_candle_low=100.50,
            oi_change_15m_pct=0.25, oi_change_5m_pct=0.08,
            oi_acceleration_5m_pct_points=0.03, global_lsr=1.08,
            lsr_change_5m_pct=-0.20, taker_buy_sell_ratio=1.20, volume_ratio=1.20,
        ))
        self.assertEqual(breakout.type, "BREAKOUT_DETECTED")
        signal = strategy.observe(market(
            price=101.15, ts=1_600_000, candle_open_time_ms=1_600_000,
            candle_open=101.02, candle_close=101.15, candle_high=101.25, candle_low=100.95,
            previous_candle_close=101.10, previous_candle_high=101.20, previous_candle_low=100.80,
            oi_change_15m_pct=0.20, oi_change_5m_pct=0.0,
            global_lsr=1.05, lsr_change_5m_pct=0.0,
            taker_buy_sell_ratio=1.10, volume_ratio=1.0,
        ))
        self.assertEqual(signal.type, "SIGNAL")
        self.assertEqual(signal.payload["setup"], "FLOW_LONG_CAMPAIGN")
        self.assertEqual(signal.direction, "LONG")

    def test_dot_like_pause_is_not_structural_reversal(self):
        strategy = FlowStructuralReversalStrategy(SETTINGS)
        armed = strategy.observe(market(
            price=101.2, price_change_15m_pct=1.3, oi_change_15m_pct=0.63,
            global_lsr=2.12, taker_buy_sell_ratio=3.0, volume_ratio=4.3,
            prior_high=101.0, candle_high=101.4, candle_low=100.8,
            candle_open=100.8, candle_close=101.2,
        ))
        self.assertEqual(armed.type, "EXTREME_ARMED")
        pause = market(
            price=100.9, ts=1_300_000, candle_open_time_ms=1_300_000,
            price_change_15m_pct=0.7, oi_change_15m_pct=0.69, oi_change_5m_pct=0.02,
            global_lsr=2.13, lsr_change_5m_pct=-0.4,
            taker_buy_sell_ratio=0.65, volume_ratio=4.4,
            candle_open=101.2, candle_close=100.8, candle_high=101.3, candle_low=100.7,
            previous_candle_close=101.2, previous_candle_high=101.4, previous_candle_low=100.6,
        )
        self.assertIsNone(strategy.observe(pause))
        checks = strategy.diagnose(pause)["checks"]
        self.assertFalse(checks["choch"])
        self.assertFalse(checks["oi_15m_deteriorates"])

    def test_reversal_requires_choch_then_another_candle_holding_structure(self):
        strategy = FlowStructuralReversalStrategy(SETTINGS)
        strategy.observe(market(
            price=101.2, price_change_15m_pct=1.3, oi_change_15m_pct=0.60,
            global_lsr=1.80, taker_buy_sell_ratio=2.0, volume_ratio=2.0,
            prior_high=101.0, candle_high=101.4, candle_low=100.8,
            candle_open=100.8, candle_close=101.2,
        ))
        choch = strategy.observe(market(
            price=100.35, ts=1_300_000, candle_open_time_ms=1_300_000,
            price_change_15m_pct=0.2, oi_change_15m_pct=0.30, oi_change_5m_pct=-0.10,
            global_lsr=1.65, lsr_change_5m_pct=-0.20,
            taker_buy_sell_ratio=0.80, volume_ratio=1.0,
            candle_open=101.0, candle_close=100.35, candle_high=101.0, candle_low=100.2,
            previous_candle_close=101.2, previous_candle_high=101.4, previous_candle_low=100.6,
        ))
        self.assertEqual(choch.type, "CHOCH_DETECTED")
        hold = strategy.observe(market(
            price=100.20, ts=1_600_000, candle_open_time_ms=1_600_000,
            price_change_15m_pct=-0.2, oi_change_15m_pct=0.20, oi_change_5m_pct=-0.05,
            global_lsr=1.60, lsr_change_5m_pct=-0.10,
            taker_buy_sell_ratio=0.90, volume_ratio=0.80,
            candle_open=100.35, candle_close=100.20, candle_high=100.50, candle_low=100.0,
            previous_candle_close=100.35, previous_candle_high=101.0, previous_candle_low=100.2,
        ))
        self.assertEqual(hold.type, "SIGNAL")
        self.assertEqual(hold.payload["setup"], "FLOW_STRUCTURAL_REVERSAL")
        self.assertEqual(hold.direction, "SHORT")


if __name__ == "__main__":
    unittest.main()
