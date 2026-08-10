import unittest

from engine.binance import MarketSnapshot
from engine.volatility_scalp import VolatilityExhaustionFadeScalpStrategy


def market(**overrides):
    values = dict(
        symbol="TESTUSDT", timestamp_ms=1_190_000, price=99.5,
        price_change_15m_pct=-1.0, oi_change_15m_pct=0.0, global_lsr=1.0,
        taker_buy_sell_ratio=1.0, funding_rate_pct=0.0, volume_ratio=1.0,
        spread_pct=0.02, prior_high=101.0, prior_low=99.0,
        candle_open=100.0, candle_high=100.1, candle_low=99.3, candle_close=99.5,
        candle_open_time_ms=900_000, directional_candle_count=2,
    )
    values.update(overrides)
    return MarketSnapshot(**values)


class VolatilityScalpTests(unittest.TestCase):
    SETTINGS = {"volatility_fade_scalp": {
        "enabled": True, "min_directional_candles": 2, "min_body_pct": 0.40,
        "min_rejection_wick_fraction": 0.15, "min_candle_age_seconds": 240,
        "max_spread_pct": 0.08, "target_pct": 0.80, "stop_pct": 2.0,
    }}

    def test_red_exhaustion_near_candle_close_signals_long(self):
        strategy = VolatilityExhaustionFadeScalpStrategy(self.SETTINGS)
        event = strategy.observe(market())
        self.assertEqual(event.type, "SIGNAL")
        self.assertEqual(event.direction, "LONG")
        self.assertEqual(event.payload["setup"], strategy.setup)
        self.assertAlmostEqual(event.payload["target_price"], 100.296)
        self.assertAlmostEqual(event.payload["stop_price"], 97.51)

    def test_prefilter_is_independent_from_dump_and_border_gates(self):
        strategy = VolatilityExhaustionFadeScalpStrategy(self.SETTINGS)
        context = {
            "candle_open": 100.0, "candle_close": 99.5,
            "candle_high": 100.1, "candle_low": 99.3,
            "directional_candle_count": 2, "passes": False,
        }
        self.assertTrue(strategy.prefilter_context(context))

    def test_signal_is_not_emitted_early_or_twice_in_same_candle(self):
        strategy = VolatilityExhaustionFadeScalpStrategy(self.SETTINGS)
        self.assertIsNone(strategy.observe(market(timestamp_ms=1_100_000)))
        self.assertIsNotNone(strategy.observe(market()))
        self.assertIsNone(strategy.observe(market(timestamp_ms=1_195_000)))

    def test_deferred_fade_reprices_after_two_minutes(self):
        settings = {"volatility_fade_scalp": {
            **self.SETTINGS["volatility_fade_scalp"],
            "fade_deferred_confirmation_seconds": 120,
            "fade_deferred_confirmation_ttl_seconds": 300,
            "min_reversal_confirmation_score": 1,
            "direct_reversal_confirmation_required": True,
            "direct_price_reversal_required": True,
            "reversal_long_min_taker": 1.05,
        }}
        strategy = VolatilityExhaustionFadeScalpStrategy(settings)
        armed = market(taker_buy_sell_ratio=1.10, price_change_5m_pct=0.10)
        self.assertIsNone(strategy.observe(armed))
        self.assertIn("TESTUSDT", strategy.deferred_fades)
        self.assertIsNone(strategy.observe(market(
            timestamp_ms=1_309_000, price=99.55,
            taker_buy_sell_ratio=1.10, price_change_5m_pct=0.10,
        )))
        confirmed = strategy.observe(market(
            timestamp_ms=1_310_000, price=99.60,
            taker_buy_sell_ratio=1.10, price_change_5m_pct=0.10,
        ))
        self.assertIsNotNone(confirmed)
        self.assertEqual(confirmed.direction, "LONG")
        self.assertAlmostEqual(confirmed.payload["entry_price"], 99.60)
        self.assertEqual(
            confirmed.payload["evidence"]["deferred_confirmation"]["age_seconds"],
            120.0,
        )

    def test_deferred_fade_reprices_even_when_flow_changes(self):
        settings = {"volatility_fade_scalp": {
            **self.SETTINGS["volatility_fade_scalp"],
            "fade_deferred_confirmation_seconds": 120,
            "fade_deferred_confirmation_ttl_seconds": 300,
            "min_reversal_confirmation_score": 1,
            "direct_reversal_confirmation_required": True,
            "direct_price_reversal_required": True,
            "reversal_short_max_taker": 0.95,
            "continuation_enabled": True,
        }}
        strategy = VolatilityExhaustionFadeScalpStrategy(settings)
        armed = market(
            price=100.5, price_change_15m_pct=1.0, price_change_5m_pct=-0.10,
            candle_open=100.0, candle_close=100.5,
            candle_high=100.7, candle_low=99.9,
            taker_buy_sell_ratio=0.90, lsr_change_15m_pct=-0.5,
        )
        self.assertIsNone(strategy.observe(armed))
        self.assertIn("TESTUSDT", strategy.deferred_fades)
        repriced = strategy.observe(market(
            timestamp_ms=1_310_000, price=101.0,
            price_change_15m_pct=1.5, price_change_5m_pct=0.60,
            candle_open=100.5, candle_close=101.0,
            candle_high=101.1, candle_low=100.4,
            taker_buy_sell_ratio=1.20,
        ))
        self.assertIsNotNone(repriced)
        self.assertEqual(repriced.direction, "SHORT")
        self.assertAlmostEqual(repriced.payload["entry_price"], 101.0)
        self.assertNotIn("TESTUSDT", strategy.deferred_fades)

    def test_adaptive_stop_uses_atr_but_respects_configured_cap(self):
        settings = {"volatility_fade_scalp": {
            **self.SETTINGS["volatility_fade_scalp"],
            "adaptive_stop_enabled": True,
            "adaptive_stop_atr_multiple": 0.90,
            "adaptive_stop_max_pct": 2.50,
        }}
        strategy = VolatilityExhaustionFadeScalpStrategy(settings)
        event = strategy.observe(market(atr14_pct=2.60))
        self.assertAlmostEqual(event.payload["risk_pct"], 2.34)
        self.assertAlmostEqual(event.payload["stop_price"], 99.5 * (1 - 0.0234))

        strategy = VolatilityExhaustionFadeScalpStrategy(settings)
        capped = strategy.observe(market(atr14_pct=4.00))
        self.assertAlmostEqual(capped.payload["risk_pct"], 2.50)

    def test_reversal_requires_at_least_one_real_confirmation_when_enabled(self):
        settings = {"volatility_fade_scalp": {
            **self.SETTINGS["volatility_fade_scalp"],
            "min_reversal_confirmation_score": 1,
        }}
        strategy = VolatilityExhaustionFadeScalpStrategy(settings)
        blocked = market(
            taker_buy_sell_ratio=0.80, oi_change_15m_pct=-0.20,
            lsr_change_15m_pct=-0.30, ema21_slope_pct=-0.10,
        )
        self.assertIsNone(strategy.observe(blocked))

        strategy = VolatilityExhaustionFadeScalpStrategy(settings)
        confirmed = market(
            taker_buy_sell_ratio=1.05, oi_change_15m_pct=-0.20,
            lsr_change_15m_pct=-0.30, ema21_slope_pct=-0.10,
        )
        event = strategy.observe(confirmed)
        self.assertEqual(event.direction, "LONG")
        self.assertEqual(event.payload["evidence"]["reversal_confirmation_score"], 1)

    def test_fade_requires_direct_flow_or_price_reversal_when_configured(self):
        settings = {"volatility_fade_scalp": {
            **self.SETTINGS["volatility_fade_scalp"],
            "min_reversal_confirmation_score": 2,
            "direct_reversal_confirmation_required": True,
            "continuation_enabled": True,
        }}
        strategy = VolatilityExhaustionFadeScalpStrategy(settings)
        # OI expansion plus falling LSR gives two contextual SHORT votes, but
        # taker flow and EMA are still driving the green candle upward.
        event = strategy.observe(market(
            price=100.5, price_change_15m_pct=1.0,
            candle_open=100.0, candle_close=100.5,
            candle_high=100.7, candle_low=99.9,
            oi_change_15m_pct=0.10, oi_change_5m_pct=0.05,
            lsr_change_15m_pct=-1.0,
            taker_buy_sell_ratio=1.30, ema21_slope_pct=0.10,
        ))
        self.assertIsNone(event)
        self.assertEqual(
            strategy.states["TESTUSDT"]["arm_reason"],
            "missing_direct_reversal_confirmation",
        )

    def test_score_zero_arms_continuation_and_waits_for_micro_rebreak(self):
        settings = {"volatility_fade_scalp": {
            **self.SETTINGS["volatility_fade_scalp"],
            "min_reversal_confirmation_score": 1,
            "continuation_enabled": True,
            "continuation_min_age_seconds": 45,
            "continuation_ttl_seconds": 600,
            "continuation_short_max_taker": 0.97,
            "continuation_min_oi_5m_pct": -0.05,
        }}
        strategy = VolatilityExhaustionFadeScalpStrategy(settings)
        armed = market(
            taker_buy_sell_ratio=0.80, oi_change_15m_pct=-0.20,
            lsr_change_15m_pct=-0.30, ema21_slope_pct=-0.10,
        )
        self.assertIsNone(strategy.observe(armed))
        continued = market(
            timestamp_ms=1_250_000, price=99.2,
            taker_buy_sell_ratio=0.85, oi_change_5m_pct=0.01,
            volume_growth_ratio=1.0, edge_position=0.5,
            micro_open=99.4, micro_close=99.1, micro_previous_low=99.2,
            micro_previous_high=99.6,
        )
        event = strategy.observe(continued)
        self.assertEqual(event.direction, "SHORT")
        self.assertEqual(event.payload["setup"], strategy.continuation_setup)

    def test_fade_does_not_use_ema_as_direct_vote_against_strong_taker_flow(self):
        settings = {"volatility_fade_scalp": {
            **self.SETTINGS["volatility_fade_scalp"],
            "min_reversal_confirmation_score": 2,
            "direct_reversal_confirmation_required": True,
            "continuation_enabled": True,
            "reversal_long_min_taker": 1.05,
            "ema_confirmation_min_taker": 0.90,
            "ema_confirmation_max_taker": 1.10,
            "ema_confirmation_max_adx": 50.0,
        }}
        strategy = VolatilityExhaustionFadeScalpStrategy(settings)
        # Contextual OI/LSR and a positive EMA are not enough to buy while
        # aggressive sellers still dominate (ACE-like losing fade).
        event = strategy.observe(market(
            taker_buy_sell_ratio=0.75,
            oi_change_15m_pct=0.32, oi_change_5m_pct=0.24,
            lsr_change_15m_pct=0.50, ema21_slope_pct=0.05, adx14=31.7,
        ))
        self.assertIsNone(event)
        self.assertEqual(
            strategy.states["TESTUSDT"]["arm_reason"],
            "missing_direct_reversal_confirmation",
        )

    def test_fade_does_not_use_ema_in_high_adx_without_taker_flip(self):
        settings = {"volatility_fade_scalp": {
            **self.SETTINGS["volatility_fade_scalp"],
            "min_reversal_confirmation_score": 2,
            "direct_reversal_confirmation_required": True,
            "continuation_enabled": True,
            "reversal_short_max_taker": 0.95,
            "ema_confirmation_min_taker": 0.90,
            "ema_confirmation_max_taker": 1.10,
            "ema_confirmation_max_adx": 50.0,
        }}
        strategy = VolatilityExhaustionFadeScalpStrategy(settings)
        event = strategy.observe(market(
            price=100.5, price_change_15m_pct=1.0,
            candle_open=100.0, candle_close=100.5,
            candle_high=100.7, candle_low=99.9,
            taker_buy_sell_ratio=1.45,
            oi_change_15m_pct=0.20, oi_change_5m_pct=0.10,
            lsr_change_15m_pct=-0.40, ema21_slope_pct=-0.30, adx14=66.5,
        ))
        self.assertIsNone(event)
        self.assertEqual(
            strategy.states["TESTUSDT"]["arm_reason"],
            "missing_direct_reversal_confirmation",
        )

    def test_fade_requires_price_turn_instead_of_taker_alone(self):
        settings = {"volatility_fade_scalp": {
            **self.SETTINGS["volatility_fade_scalp"],
            "min_reversal_confirmation_score": 2,
            "direct_reversal_confirmation_required": True,
            "direct_price_reversal_required": True,
            "reversal_short_max_taker": 0.95,
            "continuation_enabled": True,
        }}
        strategy = VolatilityExhaustionFadeScalpStrategy(settings)
        event = strategy.observe(market(
            price=100.8, price_change_15m_pct=0.97, price_change_5m_pct=0.56,
            candle_open=100.0, candle_close=100.6, candle_high=101.0, candle_low=99.9,
            taker_buy_sell_ratio=0.918, oi_change_15m_pct=0.10,
            oi_change_5m_pct=0.05, lsr_change_15m_pct=-0.50,
            ema21_slope_pct=0.06, adx14=60.7,
            micro_open=100.5, micro_close=100.7,
            micro_previous_high=100.6, micro_previous_low=100.3,
        ))
        self.assertIsNone(event)
        self.assertEqual(
            strategy.states["TESTUSDT"]["arm_reason"],
            "missing_price_reversal_confirmation",
        )

    def test_extreme_liquidation_candle_is_not_faded(self):
        settings = {"volatility_fade_scalp": {
            **self.SETTINGS["volatility_fade_scalp"],
            "continuation_enabled": True,
            "extreme_volatility_guard_enabled": True,
            "max_entry_atr_pct": 6.0,
            "breakout_trend_guard_enabled": False,
        }}
        strategy = VolatilityExhaustionFadeScalpStrategy(settings)
        event = strategy.observe(market(
            price=95.0, price_change_15m_pct=-4.62, price_change_5m_pct=-0.73,
            candle_open=100.0, candle_close=95.0, candle_high=100.2, candle_low=94.0,
            taker_buy_sell_ratio=1.44, oi_change_15m_pct=-2.28,
            ema21_slope_pct=-1.45, atr14_pct=13.68,
        ))
        self.assertIsNone(event)
        self.assertEqual(strategy.states["TESTUSDT"]["arm_reason"], "extreme_volatility")

    def test_continuation_rejects_low_volume_or_chased_macro_move(self):
        settings = {"volatility_fade_scalp": {
            **self.SETTINGS["volatility_fade_scalp"],
            "continuation_enabled": True,
            "continuation_min_age_seconds": 45,
            "continuation_max_directional_15m_pct": 2.50,
            "continuation_min_volume_growth_ratio": 0.75,
            "continuation_long_max_edge_position": 0.85,
        }}

        def armed_strategy():
            strategy = VolatilityExhaustionFadeScalpStrategy(settings)
            strategy._arm_continuation(
                market(timestamp_ms=1_190_000), "LONG", 0, {}, "test",
            )
            return strategy

        common = dict(
            timestamp_ms=1_250_000, price=99.7,
            taker_buy_sell_ratio=1.10, oi_change_5m_pct=0.05,
            micro_open=99.4, micro_close=99.7, micro_previous_high=99.6,
            micro_previous_low=99.2,
        )
        low_volume_event = armed_strategy().observe(market(
            **common, volume_growth_ratio=0.57, edge_position=0.50,
            price_change_15m_pct=1.0,
        ))
        self.assertNotEqual(
            getattr(low_volume_event, "payload", {}).get("setup"),
            VolatilityExhaustionFadeScalpStrategy.continuation_setup,
        )
        chased_event = armed_strategy().observe(market(
            **common, volume_growth_ratio=1.98, edge_position=0.883,
            price_change_15m_pct=2.70,
        ))
        self.assertNotEqual(
            getattr(chased_event, "payload", {}).get("setup"),
            VolatilityExhaustionFadeScalpStrategy.continuation_setup,
        )

    def test_long_continuation_rejects_negative_5m_and_unwinding_15m_oi(self):
        settings = {"volatility_fade_scalp": {
            **self.SETTINGS["volatility_fade_scalp"],
            "continuation_enabled": True, "continuation_min_age_seconds": 45,
            "continuation_require_price_5m_alignment": True,
            "continuation_min_directional_price_5m_pct": 0.05,
            "continuation_require_oi_15m_support": True,
            "continuation_min_oi_15m_pct": -0.10,
        }}
        strategy = VolatilityExhaustionFadeScalpStrategy(settings)
        strategy._arm_continuation(market(timestamp_ms=1_190_000), "LONG", 1, {}, "test")
        event = strategy.observe(market(
            timestamp_ms=1_250_000, price=100.2,
            price_change_15m_pct=1.54, price_change_5m_pct=-0.83,
            oi_change_15m_pct=-0.24, oi_change_5m_pct=0.01,
            taker_buy_sell_ratio=1.82, volume_growth_ratio=1.37,
            edge_position=0.67, micro_open=100.0, micro_close=100.2,
            micro_previous_high=100.1, micro_previous_low=99.9,
        ))
        self.assertNotEqual(
            getattr(event, "payload", {}).get("setup"), strategy.continuation_setup,
        )

    def test_single_confirmation_arms_continuation_instead_of_fading_immediately(self):
        settings = {"volatility_fade_scalp": {
            **self.SETTINGS["volatility_fade_scalp"],
            "min_reversal_confirmation_score": 2,
            "continuation_enabled": True,
        }}
        strategy = VolatilityExhaustionFadeScalpStrategy(settings)
        weak_reversal = market(
            taker_buy_sell_ratio=1.05, oi_change_15m_pct=-0.20,
            lsr_change_15m_pct=-0.30, ema21_slope_pct=-0.10,
        )
        self.assertIsNone(strategy.observe(weak_reversal))
        self.assertEqual(strategy.states["TESTUSDT"]["reversal_score"], 1)
        self.assertEqual(strategy.states["TESTUSDT"]["direction"], "SHORT")

    def test_broken_rejection_does_not_fade_and_arms_breakout_continuation(self):
        settings = {"volatility_fade_scalp": {
            **self.SETTINGS["volatility_fade_scalp"],
            "min_reversal_confirmation_score": 2,
            "continuation_enabled": True,
            "max_rejection_break_pct": 0.10,
        }}
        strategy = VolatilityExhaustionFadeScalpStrategy(settings)
        sky_like = market(
            price=116.47,
            candle_open=113.06, candle_high=115.25,
            candle_low=112.56, candle_close=113.88,
            directional_candle_count=4,
            taker_buy_sell_ratio=1.3617, oi_change_15m_pct=-0.896,
            lsr_change_15m_pct=-0.601, ema21_slope_pct=-0.442,
            spread_pct=0.0687,
        )

        self.assertIsNone(strategy.observe(sky_like))
        self.assertEqual(strategy.states["TESTUSDT"]["direction"], "LONG")
        self.assertEqual(
            strategy.states["TESTUSDT"]["arm_reason"],
            "rejection_extreme_broken",
        )

    def test_repeated_scan_preserves_continuation_arm_time(self):
        settings = {"volatility_fade_scalp": {
            **self.SETTINGS["volatility_fade_scalp"],
            "min_reversal_confirmation_score": 2,
            "continuation_enabled": True,
        }}
        strategy = VolatilityExhaustionFadeScalpStrategy(settings)
        weak = market(
            taker_buy_sell_ratio=1.05, oi_change_15m_pct=-0.20,
            lsr_change_15m_pct=-0.30, ema21_slope_pct=-0.10,
        )
        self.assertIsNone(strategy.observe(weak))
        first_armed_at = strategy.states["TESTUSDT"]["armed_at_ms"]

        self.assertIsNone(strategy.observe(market(
            timestamp_ms=1_210_000,
            taker_buy_sell_ratio=1.05, oi_change_15m_pct=-0.20,
            lsr_change_15m_pct=-0.30, ema21_slope_pct=-0.10,
        )))
        self.assertEqual(strategy.states["TESTUSDT"]["armed_at_ms"], first_armed_at)

    def test_tst_like_directional_conflict_routes_to_continuation(self):
        settings = {"volatility_fade_scalp": {
            **self.SETTINGS["volatility_fade_scalp"],
            "min_reversal_confirmation_score": 2,
            "continuation_enabled": True,
            "directional_conflict_guard_enabled": True,
            "directional_conflict_min_impulse_pct": 1.0,
            "directional_conflict_min_oi_15m_pct": 0.5,
        }}
        strategy = VolatilityExhaustionFadeScalpStrategy(settings)
        tst_like = market(
            price=0.01549953,
            candle_open=0.01552, candle_high=0.01576,
            candle_low=0.01546, candle_close=0.01568,
            directional_candle_count=3,
            price_change_15m_pct=5.02,
            oi_change_15m_pct=1.17,
            taker_buy_sell_ratio=1.1982,
            lsr_change_15m_pct=-2.31,
            ema21_slope_pct=0.002,
            micro_open=0.01557, micro_close=0.01552,
            micro_previous_high=0.01562, micro_previous_low=0.01552,
        )

        self.assertIsNone(strategy.observe(tst_like))
        self.assertEqual(strategy.states["TESTUSDT"]["direction"], "LONG")
        self.assertEqual(
            strategy.states["TESTUSDT"]["arm_reason"],
            "directional_oi_taker_conflict",
        )

    def test_small_oi_reversal_like_winning_sky_is_not_blocked(self):
        settings = {"volatility_fade_scalp": {
            **self.SETTINGS["volatility_fade_scalp"],
            "min_reversal_confirmation_score": 2,
            "directional_conflict_guard_enabled": True,
            "directional_conflict_min_impulse_pct": 1.0,
            "directional_conflict_min_oi_15m_pct": 0.5,
        }}
        strategy = VolatilityExhaustionFadeScalpStrategy(settings)
        sky_like = market(
            price=0.1173,
            candle_open=0.11809, candle_high=0.11866,
            candle_low=0.11633, candle_close=0.11751,
            directional_candle_count=2,
            price_change_15m_pct=-0.57,
            oi_change_15m_pct=0.018,
            taker_buy_sell_ratio=0.7652,
            lsr_change_15m_pct=1.13,
            ema21_slope_pct=-0.12,
            micro_open=0.1167, micro_close=0.11751,
            micro_previous_high=0.11761, micro_previous_low=0.11633,
        )

        event = strategy.observe(sky_like)
        self.assertIsNotNone(event)
        self.assertEqual(event.direction, "LONG")

    def test_cys_like_pullback_inside_pump_routes_to_long_continuation(self):
        settings = {"volatility_fade_scalp": {
            **self.SETTINGS["volatility_fade_scalp"],
            "min_reversal_confirmation_score": 2,
            "continuation_enabled": True,
            "breakout_trend_guard_enabled": True,
            "breakout_trend_min_impulse_pct": 3.0,
            "breakout_trend_min_range_extension_pct": 1.0,
            "breakout_trend_min_ema_slope_pct": 0.2,
        }}
        strategy = VolatilityExhaustionFadeScalpStrategy(settings)
        cys_like = market(
            price=0.95740919,
            candle_open=0.9578, candle_high=0.975,
            candle_low=0.9541, candle_close=0.9687,
            directional_candle_count=4,
            price_change_15m_pct=4.53,
            oi_change_15m_pct=0.018,
            taker_buy_sell_ratio=0.9233,
            lsr_change_15m_pct=-1.03,
            ema21_slope_pct=0.594,
            distance_to_prior_high_pct=-3.77,
            micro_open=0.9734, micro_close=0.9772,
            micro_previous_high=0.9749, micro_previous_low=0.9657,
        )

        self.assertIsNone(strategy.observe(cys_like))
        self.assertEqual(strategy.states["TESTUSDT"]["direction"], "LONG")
        self.assertEqual(
            strategy.states["TESTUSDT"]["arm_reason"],
            "breakout_trend_still_intact",
        )

    def test_tutu_like_pullback_after_breakout_does_not_short_the_pump(self):
        settings = {"volatility_fade_scalp": {
            **self.SETTINGS["volatility_fade_scalp"],
            "min_reversal_confirmation_score": 2,
            "continuation_enabled": True,
            "breakout_trend_guard_enabled": True,
            "breakout_trend_min_impulse_pct": 2.5,
            "breakout_trend_min_range_extension_pct": 1.0,
            "breakout_trend_min_ema_slope_pct": 0.2,
        }}
        strategy = VolatilityExhaustionFadeScalpStrategy(settings)
        tutu = market(
            price=0.17386474, price_change_15m_pct=7.963,
            oi_change_15m_pct=0.064, oi_change_5m_pct=0.317,
            taker_buy_sell_ratio=0.9955, lsr_change_15m_pct=-4.597,
            ema21_slope_pct=0.721, prior_high=0.18,
            candle_open=0.17144, candle_high=0.18614,
            candle_low=0.16979, candle_close=0.17598,
            directional_candle_count=3,
        )
        self.assertIsNone(strategy.observe(tutu))
        self.assertEqual(strategy.states["TESTUSDT"]["direction"], "LONG")
        self.assertEqual(strategy.states["TESTUSDT"]["arm_reason"], "breakout_trend_still_intact")

    def test_bmt_like_oi_unwind_and_seller_flow_does_not_confirm_long(self):
        settings = {"volatility_fade_scalp": {
            **self.SETTINGS["volatility_fade_scalp"],
            "min_reversal_confirmation_score": 2,
            "continuation_enabled": True,
            "recent_flow_guard_enabled": True,
            "recent_flow_min_price_5m_pct": 0.40,
            "recent_flow_max_oi_5m_pct": -0.50,
        }}
        strategy = VolatilityExhaustionFadeScalpStrategy(settings)
        bmt = market(
            price=0.02627649, price_change_15m_pct=-0.262,
            price_change_5m_pct=-0.486,
            oi_change_15m_pct=2.049, oi_change_5m_pct=-1.077,
            taker_buy_sell_ratio=0.9693, lsr_change_15m_pct=-2.227,
            ema21_slope_pct=0.518,
            candle_open=0.02677, candle_high=0.02693,
            candle_low=0.02546, candle_close=0.02662,
            micro_open=0.02661, micro_close=0.02635,
            directional_candle_count=2,
        )
        self.assertIsNone(strategy.observe(bmt))
        self.assertEqual(strategy.states["TESTUSDT"]["direction"], "SHORT")
        self.assertEqual(strategy.states["TESTUSDT"]["arm_reason"], "recent_flow_still_directional")

    def test_hmstr_like_short_build_blocks_long_even_with_positive_ema(self):
        settings = {"volatility_fade_scalp": {
            **self.SETTINGS["volatility_fade_scalp"],
            "min_reversal_confirmation_score": 2,
            "continuation_enabled": True,
            "directional_position_build_guard_enabled": True,
            "directional_position_build_min_price_5m_pct": 0.40,
            "directional_position_build_min_oi_15m_pct": 0.50,
            "directional_position_build_min_oi_5m_pct": 0.00,
            "directional_position_build_min_adx": 35.0,
            "directional_position_build_long_max_taker": 0.80,
        }}
        strategy = VolatilityExhaustionFadeScalpStrategy(settings)
        hmstr = market(
            price=0.00021887,
            price_change_15m_pct=-0.598, price_change_5m_pct=-0.735,
            oi_change_15m_pct=1.163, oi_change_5m_pct=0.038,
            taker_buy_sell_ratio=0.3815, lsr_change_15m_pct=-1.005,
            ema21_slope_pct=0.118, adx14=48.86,
            candle_open=0.0002179, candle_high=0.000218,
            candle_low=0.0002154, candle_close=0.0002162,
            micro_open=0.0002166, micro_close=0.0002184,
            directional_candle_count=2,
        )
        self.assertIsNone(strategy.observe(hmstr))
        self.assertEqual(strategy.states["TESTUSDT"]["direction"], "SHORT")
        self.assertEqual(strategy.states["TESTUSDT"]["arm_reason"], "directional_position_build")

    def test_symmetric_long_build_blocks_short_fade(self):
        settings = {"volatility_fade_scalp": {
            **self.SETTINGS["volatility_fade_scalp"],
            "min_reversal_confirmation_score": 2,
            "continuation_enabled": True,
            "directional_position_build_guard_enabled": True,
            "directional_conflict_guard_enabled": False,
        }}
        strategy = VolatilityExhaustionFadeScalpStrategy(settings)
        pump = market(
            price=101.0, price_change_15m_pct=1.0, price_change_5m_pct=0.70,
            oi_change_15m_pct=1.20, oi_change_5m_pct=0.20,
            taker_buy_sell_ratio=1.35, adx14=42.0,
            candle_open=100.0, candle_high=101.5,
            candle_low=99.8, candle_close=101.0,
            directional_candle_count=3,
        )
        self.assertIsNone(strategy.observe(pump))
        self.assertEqual(strategy.states["TESTUSDT"]["direction"], "LONG")
        self.assertEqual(strategy.states["TESTUSDT"]["arm_reason"], "directional_position_build")


if __name__ == "__main__":
    unittest.main()
