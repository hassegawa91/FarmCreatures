import unittest

from engine.binance import MarketSnapshot
from engine.dump_reclaim import DumpExhaustionReclaimStrategy


def market(**overrides):
    values = dict(
        symbol="TESTUSDT", timestamp_ms=1_000_000, price=98.0,
        price_change_15m_pct=-2.0, oi_change_15m_pct=0.2, global_lsr=1.0,
        taker_buy_sell_ratio=1.1, funding_rate_pct=0.0, volume_ratio=2.0,
        spread_pct=0.02, prior_high=101.0, prior_low=99.0,
        candle_high=100.0, candle_low=97.5, candle_open=100.0, candle_close=98.0,
        candle_open_time_ms=900_000, previous_candle_close=100.0,
        range_width_pct=2.0, compression_ratio=1.0, atr14=2.0, atr14_pct=2.0,
        adx14=25.0, price_change_5m_pct=-2.0, lsr_change_15m_pct=-0.5,
    )
    values.update(overrides)
    return MarketSnapshot(**values)


class DumpReclaimTests(unittest.TestCase):
    def settings(self):
        return {
            "dump_reclaim_arm_price_5m_pct": 2.0,
            "dump_reclaim_silence_minutes": 5,
            "dump_reclaim_ttl_minutes": 30,
            "dump_reclaim_min_oi_15m_pct": 0.0,
            "dump_reclaim_max_lsr_15m_pct": 0.0,
            "dump_reclaim_min_taker_ratio": 1.0,
            "dump_reclaim_max_taker_ratio": 1.6,
            "dump_reclaim_max_rebound_floor_pct": 3.0,
            "dump_reclaim_max_rebound_atr_multiple": 1.5,
            "dump_reclaim_stop_pct": 1.5, "dump_reclaim_target_pct": 3.0,
            "max_spread_pct": 0.08, "dump_reclaim_cooldown_minutes": 60,
        }

    def arm(self, strategy):
        event = strategy.observe(market())
        self.assertEqual(event.type, "ARMED")

    def test_entry_waits_for_pullback_and_second_reclaim(self):
        strategy = DumpExhaustionReclaimStrategy(self.settings())
        self.arm(strategy)
        event = strategy.observe(market(
            timestamp_ms=1_300_000, candle_open_time_ms=1_200_000,
            price=98.6, candle_open=98.2, candle_close=98.6, candle_high=98.8,
            candle_low=98.0, previous_candle_close=98.0, price_change_5m_pct=0.6,
        ))
        self.assertEqual(event.type, "RECLAIM")
        event = strategy.observe(market(
            timestamp_ms=1_600_000, candle_open_time_ms=1_500_000,
            price=98.3, candle_open=98.6, candle_close=98.3, candle_high=98.7,
            candle_low=98.1, previous_candle_close=98.6, price_change_5m_pct=-0.3,
        ))
        self.assertEqual(event.type, "PULLBACK")
        event = strategy.observe(market(
            timestamp_ms=1_900_000, candle_open_time_ms=1_800_000,
            price=98.9, candle_open=98.3, candle_close=98.9, candle_high=99.0,
            candle_low=98.2, previous_candle_close=98.3, previous_candle_high=98.5,
            price_change_5m_pct=0.6,
        ))
        self.assertEqual(event.type, "SIGNAL")
        self.assertEqual(event.payload["evidence"]["entry_attempt"], 1)
        stop_pct = float(event.payload["risk_pct"])
        target_pct = (
            float(event.payload["target_price"]) / float(event.payload["entry_price"]) - 1.0
        ) * 100.0
        self.assertGreaterEqual(target_pct, max(3.0, stop_pct * 1.75) - 1e-9)

    def test_losing_stop_arms_one_controlled_reentry(self):
        strategy = DumpExhaustionReclaimStrategy(self.settings())
        signal = {
            "symbol": "TESTUSDT", "setup": strategy.reversal_setup, "stop_price": 97.0,
            "evidence": {"entry_attempt": 1, "dump_low": 96.5, "max_dump_pct": -3.0},
        }
        result = {
            "execution_id": 9, "closed_at_ms": 2_000_000,
            "exit_reason": "STOP", "net_pnl": -5.0,
        }
        event = strategy.arm_reentry(signal, result, market(
            timestamp_ms=2_000_000, candle_open_time_ms=1_800_000, candle_low=96.0,
        ))
        self.assertEqual(event.type, "ARMED")
        self.assertEqual(strategy.states["TESTUSDT"].attempt, 2)
        signal["evidence"]["entry_attempt"] = 2
        self.assertIsNone(strategy.arm_reentry(signal, result, None))

    def test_taker_climax_blocks_late_chase(self):
        strategy = DumpExhaustionReclaimStrategy(self.settings())
        self.arm(strategy)
        event = strategy.observe(market(
            timestamp_ms=1_300_000, candle_open_time_ms=1_200_000,
            price=98.6, candle_low=98.0, previous_candle_close=98.0,
            price_change_5m_pct=0.6, taker_buy_sell_ratio=2.75,
        ))
        self.assertIsNone(event)
        self.assertIn("REV:taker_not_climax", strategy.diagnose(market(
            timestamp_ms=1_310_000, candle_open_time_ms=1_200_000,
            price=98.6, candle_low=98.0, previous_candle_close=98.0,
            price_change_5m_pct=0.6, taker_buy_sell_ratio=2.75,
        ))["blocked_by"])

    def test_first_reclaim_accepts_lsr_or_taker_flow_confirmation(self):
        strategy = DumpExhaustionReclaimStrategy(self.settings())
        self.arm(strategy)
        event = strategy.observe(market(
            timestamp_ms=1_300_000, candle_open_time_ms=1_200_000,
            price=98.6, candle_open=98.2, candle_close=98.6, candle_high=98.8,
            candle_low=98.0, previous_candle_close=98.0, price_change_5m_pct=0.6,
            lsr_change_15m_pct=0.5, taker_buy_sell_ratio=1.1,
        ))
        self.assertEqual(event.type, "RECLAIM")

        blocked = DumpExhaustionReclaimStrategy(self.settings())
        self.arm(blocked)
        self.assertIsNone(blocked.observe(market(
            timestamp_ms=1_300_000, candle_open_time_ms=1_200_000,
            price=98.6, candle_low=98.0, previous_candle_close=98.0,
            price_change_5m_pct=0.6, lsr_change_15m_pct=0.5,
            taker_buy_sell_ratio=0.9,
        )))

    def test_atr_normalized_rebound_blocks_overextended_entry(self):
        strategy = DumpExhaustionReclaimStrategy(self.settings())
        self.arm(strategy)
        event = strategy.observe(market(
            timestamp_ms=1_300_000, candle_open_time_ms=1_200_000,
            price=101.0, candle_low=98.0, previous_candle_close=99.0,
            price_change_5m_pct=1.0, atr14_pct=1.0,
        ))
        self.assertIsNone(event)
        diagnosis = strategy.diagnose(market(
            timestamp_ms=1_310_000, candle_open_time_ms=1_200_000,
            price=101.0, candle_low=98.0, previous_candle_close=99.0,
            price_change_5m_pct=1.0, atr14_pct=1.0,
        ))
        self.assertIn("REV:reclaim_not_chased", diagnosis["blocked_by"])

    def test_reversal_requires_oi_acceleration_lsr_unwind_and_buyers_together(self):
        strategy = DumpExhaustionReclaimStrategy(self.settings())
        self.arm(strategy)
        strategy.observe(market(
            timestamp_ms=1_300_000, candle_open_time_ms=1_200_000,
            price=98.6, candle_open=98.2, candle_close=98.6, candle_high=98.8,
            candle_low=98.0, previous_candle_close=98.0, price_change_5m_pct=0.6,
        ))
        strategy.observe(market(
            timestamp_ms=1_600_000, candle_open_time_ms=1_500_000,
            price=98.3, candle_open=98.6, candle_close=98.3, candle_high=98.7,
            candle_low=98.1, previous_candle_close=98.6, price_change_5m_pct=-0.3,
        ))
        self.assertIsNone(strategy.observe(market(
            timestamp_ms=1_900_000, candle_open_time_ms=1_800_000,
            price=98.9, candle_open=98.3, candle_close=98.9, candle_high=99.0,
            candle_low=98.2, previous_candle_close=98.3, price_change_5m_pct=0.6,
            oi_acceleration_5m_pct_points=-0.1, lsr_change_5m_pct=0.2,
            taker_buy_sell_ratio=0.9,
        )))

    def test_failed_rebound_breaks_low_and_signals_continuation_short(self):
        settings = {
            **self.settings(), "dump_continuation_min_rebound_pct": 0.5,
            "dump_continuation_max_taker_ratio": 0.9,
            "dump_continuation_min_stop_pct": 1.2,
            "dump_continuation_max_stop_pct": 2.5,
            "dump_continuation_target_r": 1.75,
        }
        strategy = DumpExhaustionReclaimStrategy(settings)
        self.arm(strategy)
        # Broad rebound is recorded, but it is not yet a directional entry.
        event = strategy.observe(market(
            timestamp_ms=1_300_000, candle_open_time_ms=1_200_000,
            price=98.6, candle_open=98.2, candle_close=98.6, candle_high=98.8,
            candle_low=98.0, previous_candle_close=98.0, price_change_5m_pct=0.6,
        ))
        self.assertEqual(event.type, "RECLAIM")
        event = strategy.observe(market(
            timestamp_ms=1_600_000, candle_open_time_ms=1_500_000,
            price=97.2, candle_open=98.4, candle_close=97.2, candle_high=98.5,
            candle_low=97.1, previous_candle_close=98.4, price_change_5m_pct=-1.2,
            taker_buy_sell_ratio=0.72, oi_change_15m_pct=0.2,
            oi_acceleration_5m_pct_points=0.1,
        ))
        self.assertEqual(event.type, "SIGNAL")
        self.assertEqual(event.direction, "SHORT")
        self.assertEqual(event.payload["setup"], strategy.continuation_setup)
        target_pct = abs(
            (float(event.payload["target_price"]) / float(event.payload["entry_price"]) - 1.0) * 100.0
        )
        self.assertAlmostEqual(target_pct / float(event.payload["risk_pct"]), 1.75)

    def test_continuation_uses_two_of_three_flow_votes(self):
        strategy = DumpExhaustionReclaimStrategy({
            **self.settings(), "dump_continuation_min_rebound_pct": 0.5,
            "dump_continuation_max_taker_ratio": 0.9,
            "dump_continuation_min_stop_pct": 1.2,
            "dump_continuation_max_stop_pct": 2.5,
            "dump_continuation_min_flow_votes": 2,
        })
        self.arm(strategy)
        strategy.observe(market(
            timestamp_ms=1_300_000, candle_open_time_ms=1_200_000,
            price=98.6, candle_open=98.2, candle_close=98.6, candle_high=98.8,
            candle_low=98.0, previous_candle_close=98.0, price_change_5m_pct=0.6,
        ))
        event = strategy.observe(market(
            timestamp_ms=1_600_000, candle_open_time_ms=1_500_000,
            price=97.6, candle_open=98.4, candle_close=97.6, candle_high=98.5,
            candle_low=97.4, previous_candle_close=98.4, previous_candle_low=98.0,
            price_change_5m_pct=-0.8, taker_buy_sell_ratio=0.85,
            oi_change_15m_pct=-0.5, oi_acceleration_5m_pct_points=-0.1,
            trades_ratio=1.0,
        ))
        self.assertEqual(event.type, "SIGNAL")
        self.assertTrue(event.payload["evidence"]["confirmation_checks"]["flow_score"])

    def test_continuation_requires_actual_seller_taker_flow(self):
        strategy = DumpExhaustionReclaimStrategy({
            **self.settings(), "dump_continuation_min_rebound_pct": 0.5,
            "dump_continuation_max_taker_ratio": 0.9,
            "dump_continuation_min_flow_votes": 2,
        })
        self.arm(strategy)
        strategy.observe(market(
            timestamp_ms=1_300_000, candle_open_time_ms=1_200_000,
            price=98.6, candle_open=98.2, candle_close=98.6, candle_high=98.8,
            candle_low=98.0, previous_candle_close=98.0, price_change_5m_pct=0.6,
        ))
        event = strategy.observe(market(
            timestamp_ms=1_600_000, candle_open_time_ms=1_500_000,
            price=97.6, candle_open=98.4, candle_close=97.6, candle_high=98.5,
            candle_low=97.4, previous_candle_close=98.4, previous_candle_low=98.0,
            price_change_5m_pct=-0.8, taker_buy_sell_ratio=1.15,
            oi_change_15m_pct=0.2, oi_acceleration_5m_pct_points=0.1,
            trades_ratio=1.0,
        ))
        self.assertNotEqual(event.type, "SIGNAL")
        self.assertIn("CONT:seller_taker", strategy.diagnose(market(
            timestamp_ms=1_610_000, candle_open_time_ms=1_500_000,
            price=97.6, candle_open=98.4, candle_close=97.6, candle_high=98.5,
            candle_low=97.4, previous_candle_close=98.4, previous_candle_low=98.0,
            price_change_5m_pct=-0.8, taker_buy_sell_ratio=1.15,
            oi_change_15m_pct=0.2, oi_acceleration_5m_pct_points=0.1,
            trades_ratio=1.0,
        ))["blocked_by"])

    def test_continuation_rejects_an_old_dump_even_if_late_flow_reappears(self):
        strategy = DumpExhaustionReclaimStrategy({
            **self.settings(), "dump_continuation_min_rebound_pct": 0.5,
            "dump_continuation_max_taker_ratio": 0.9,
            "dump_continuation_fresh_context_enabled": True,
            "dump_continuation_max_dump_age_minutes": 10,
        })
        self.arm(strategy)
        state = strategy.states["TESTUSDT"]
        state.rebound_seen = True
        state.rebound_candle_ms = 1_200_000
        checks = strategy._continuation_checks(state, market(
            timestamp_ms=1_800_001, candle_open_time_ms=1_800_000,
            price=97.6, candle_open=98.4, candle_close=97.6,
            candle_high=98.5, candle_low=97.4,
            previous_candle_close=98.4, previous_candle_low=98.0,
            price_change_5m_pct=-0.8, taker_buy_sell_ratio=0.8,
            oi_acceleration_5m_pct_points=0.1, trades_ratio=1.0,
        ))
        self.assertFalse(checks["fresh_dump_context"])

    def test_continuation_requires_a_current_bearish_impulse_when_enabled(self):
        strategy = DumpExhaustionReclaimStrategy({
            **self.settings(), "dump_continuation_min_rebound_pct": 0.5,
            "dump_continuation_max_taker_ratio": 0.9,
            "dump_continuation_require_current_impulse": True,
            "dump_continuation_min_current_price_5m_pct": 0.25,
        })
        self.arm(strategy)
        state = strategy.states["TESTUSDT"]
        state.rebound_seen = True
        state.rebound_candle_ms = 1_200_000
        checks = strategy._continuation_checks(state, market(
            timestamp_ms=1_600_000, candle_open_time_ms=1_500_000,
            price=97.6, candle_open=98.4, candle_close=97.6,
            candle_high=98.5, candle_low=97.4,
            previous_candle_close=98.4, previous_candle_low=98.0,
            price_change_5m_pct=-0.10, taker_buy_sell_ratio=0.8,
            oi_acceleration_5m_pct_points=0.1, trades_ratio=1.0,
        ))
        self.assertFalse(checks["current_bearish_impulse"])

    def test_continuation_rejects_live_price_after_breakout_is_chased(self):
        strategy = DumpExhaustionReclaimStrategy({
            **self.settings(), "dump_continuation_min_rebound_pct": 0.5,
            "dump_continuation_max_taker_ratio": 0.9,
            "dump_continuation_max_break_extension_pct": 1.0,
            "dump_continuation_max_break_atr_fraction": 0.75,
        })
        self.arm(strategy)
        strategy.observe(market(
            timestamp_ms=1_300_000, candle_open_time_ms=1_200_000,
            price=98.6, candle_open=98.2, candle_close=98.6, candle_high=98.8,
            candle_low=98.0, previous_candle_close=98.0, price_change_5m_pct=0.6,
        ))
        event = strategy.observe(market(
            timestamp_ms=1_600_000, candle_open_time_ms=1_500_000,
            price=95.5, candle_open=98.4, candle_close=97.3, candle_high=98.5,
            candle_low=97.1, previous_candle_close=98.4, previous_candle_low=98.0,
            price_change_5m_pct=-0.8, taker_buy_sell_ratio=0.72,
            oi_acceleration_5m_pct_points=0.1, trades_ratio=1.0,
        ))
        self.assertIsNone(event)
        self.assertIn("CONT:live_entry_not_chased", strategy.diagnose(market(
            timestamp_ms=1_610_000, candle_open_time_ms=1_500_000,
            price=95.5, candle_open=98.4, candle_close=97.3, candle_high=98.5,
            candle_low=97.1, previous_candle_close=98.4, previous_candle_low=98.0,
            taker_buy_sell_ratio=0.72, oi_acceleration_5m_pct_points=0.1,
            trades_ratio=1.0,
        ))["blocked_by"])

    def test_continuation_hard_extension_cap_blocks_bottom_chase(self):
        strategy = DumpExhaustionReclaimStrategy({
            **self.settings(), "dump_continuation_min_rebound_pct": 0.5,
            "dump_continuation_hard_max_break_extension_pct": 0.35,
        })
        self.arm(strategy)
        state = strategy.states["TESTUSDT"]
        state.rebound_seen = True
        state.rebound_candle_ms = 1_200_000
        state.rebound_high = 99.0
        checks = strategy._continuation_checks(state, market(
            timestamp_ms=1_600_000, candle_open_time_ms=1_500_000,
            price=97.10, candle_open=98.0, candle_close=97.2,
            candle_high=98.1, candle_low=97.0,
            previous_candle_close=98.0, previous_candle_low=97.8,
            taker_buy_sell_ratio=0.80, oi_change_15m_pct=0.20,
            trades_ratio=1.0,
        ))
        self.assertFalse(checks["live_entry_not_chased"])


if __name__ == "__main__":
    unittest.main()
