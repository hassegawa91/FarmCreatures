import tempfile
import unittest
from pathlib import Path

from engine.binance import MarketSnapshot
from engine.simulation import ParallelStrategyLab, SimulationLedger


def market(**overrides):
    values = dict(
        symbol="TESTUSDT", timestamp_ms=1_000_000, price=101.0,
        price_change_15m_pct=1.0, oi_change_15m_pct=2.5, global_lsr=1.0,
        taker_buy_sell_ratio=1.1, funding_rate_pct=0.01, volume_ratio=1.0,
        spread_pct=0.02, prior_high=102.0, prior_low=98.0,
        candle_high=101.2, candle_low=100.8, candle_open=100.5, candle_close=101.0,
        candle_open_time_ms=900_000, range_width_pct=1.0, compression_ratio=0.8,
        ema21=100.0, ema21_slope_pct=0.10, atr14=0.5, atr14_pct=0.5, adx14=25.0,
    )
    values.update(overrides)
    return MarketSnapshot(**values)


class SimulationTests(unittest.TestCase):
    def test_staged_fade_can_keep_probe_without_opening_add(self):
        with tempfile.TemporaryDirectory() as folder:
            lab = ParallelStrategyLab({
                "enabled": True, "fee_pct_per_side": 0.05, "slippage_pct_per_side": 0.0,
                "logical_exit": {"enabled": True},
                "staged_fade": {"enabled": True, "long_add_enabled": False,
                                "margin_usdt": 50.0, "leverage": 10,
                                "long_probe_fraction": 0.25, "long_add_trigger_r": 0.20,
                                "short_probe_fraction": 0.10},
                "trend": {"enabled": False}, "grid": {"enabled": False},
            }, Path(folder) / "lab.sqlite")
            try:
                signal = {
                    "setup": lab.VOLATILITY_FADE_SCALP, "symbol": "TESTUSDT",
                    "direction": "LONG", "timestamp_ms": 1_000_000,
                    "entry_price": 100.0, "stop_price": 98.0, "target_price": 100.8,
                    "risk_pct": 2.0, "evidence": {},
                }
                lab.on_strategy_signal(signal, market(price=100.0))
                lab.process(market(timestamp_ms=1_300_000, candle_open_time_ms=1_200_000,
                                   price=100.4, candle_high=100.45, candle_low=100.1))
                self.assertEqual(len(lab.ledger.open_rows(
                    lab.STAGED_PROBE_PREFIX + lab.VOLATILITY_FADE_SCALP,
                )), 1)
                self.assertEqual(len(lab.ledger.open_rows(
                    lab.STAGED_ADD_PREFIX + lab.VOLATILITY_FADE_SCALP,
                )), 0)
            finally:
                lab.close()

    def test_staged_fade_opens_probe_and_adds_long_after_point_two_r(self):
        with tempfile.TemporaryDirectory() as folder:
            lab = ParallelStrategyLab({
                "enabled": True, "fee_pct_per_side": 0.05, "slippage_pct_per_side": 0.0,
                "logical_exit": {"enabled": True, "min_giveback_pct": 0.40,
                                 "max_giveback_pct": 0.80, "atr_giveback_multiple": 0.80},
                "staged_fade": {"enabled": True, "margin_usdt": 50.0, "leverage": 10,
                                "long_probe_fraction": 0.25, "long_add_trigger_r": 0.20,
                                "short_probe_fraction": 0.10},
                "trend": {"enabled": False}, "grid": {"enabled": False},
            }, Path(folder) / "lab.sqlite")
            try:
                signal = {
                    "setup": lab.VOLATILITY_FADE_SCALP, "symbol": "TESTUSDT",
                    "direction": "LONG", "timestamp_ms": 1_000_000,
                    "entry_price": 100.0, "stop_price": 98.0, "target_price": 100.8,
                    "risk_pct": 2.0, "evidence": {},
                }
                lab.on_strategy_signal(signal, market(price=100.0))
                probe = lab.ledger.open_rows(lab.STAGED_PROBE_PREFIX + lab.VOLATILITY_FADE_SCALP)
                self.assertEqual(len(probe), 1)
                self.assertEqual(probe[0]["evidence"]["notional_usdt"], 125.0)
                lab.process(market(
                    timestamp_ms=1_300_000, candle_open_time_ms=1_200_000,
                    price=100.4, candle_high=100.45, candle_low=100.1,
                ))
                add = lab.ledger.open_rows(lab.STAGED_ADD_PREFIX + lab.VOLATILITY_FADE_SCALP)
                self.assertEqual(len(add), 1)
                self.assertEqual(add[0]["evidence"]["notional_usdt"], 375.0)
            finally:
                lab.close()

    def test_volatility_fade_scalp_opens_after_exhaustion_and_defers_same_candle(self):
        with tempfile.TemporaryDirectory() as folder:
            lab = ParallelStrategyLab({
                "enabled": True,
                "volatility_fade_scalp": {
                    "enabled": True, "min_directional_candles": 2,
                    "min_body_pct": 0.40, "min_rejection_wick_fraction": 0.15,
                    "target_pct": 0.80, "stop_pct": 2.0, "max_open_trades": 10,
                },
                "trend": {"enabled": False}, "grid": {"enabled": False},
            }, Path(folder) / "lab.sqlite")
            try:
                opening = market(
                    price=99.5, candle_open=100.0, candle_close=99.5,
                    candle_high=100.1, candle_low=99.3, directional_candle_count=2,
                )
                lab.process(opening)
                opened = lab.ledger.open_rows(lab.VOLATILITY_FADE_SCALP)
                self.assertEqual(len(opened), 1)
                self.assertEqual(opened[0]["direction"], "LONG")
                # Pre-entry OHLC from the same 5m candle must not stop the new trade.
                lab.process(market(
                    timestamp_ms=1_100_000, candle_open_time_ms=900_000,
                    price=99.6, candle_open=100.0, candle_close=99.6,
                    candle_high=100.0, candle_low=95.0, directional_candle_count=2,
                ))
                self.assertEqual(len(lab.ledger.open_rows(lab.VOLATILITY_FADE_SCALP)), 1)
                lab.process(market(
                    timestamp_ms=1_300_000, candle_open_time_ms=1_200_000,
                    price=100.4, candle_open=99.6, candle_close=100.4,
                    candle_high=100.5, candle_low=99.5, directional_candle_count=1,
                ))
                closed = lab.ledger.recent(1)[0]
                self.assertEqual(closed["exit_reason"], "TARGET")
            finally:
                lab.close()
    def test_cost_aware_grid_waits_for_stability_and_real_cross(self):
        with tempfile.TemporaryDirectory() as folder:
            lab = ParallelStrategyLab({
                "enabled": True,
                "trend": {},
                "grid": {"min_stable_observations": 3, "min_spacing_pct": 0.30},
            }, Path(folder) / "lab.sqlite")
            try:
                for ts in (1_000_000, 1_300_000, 1_600_000):
                    lab.process(market(
                        timestamp_ms=ts, candle_open_time_ms=ts, price=100.0,
                        candle_open=100.0, candle_close=100.0, candle_high=100.2, candle_low=99.8,
                        oi_change_15m_pct=0.10, adx14=15.0,
                    ))
                self.assertEqual(lab.status()["active_grid_sessions"], 1)
                self.assertEqual(len(lab.ledger.open_rows(lab.GRID)), 0)
                lab.process(market(
                    timestamp_ms=1_900_000, candle_open_time_ms=1_900_000, price=99.8,
                    candle_open=100.0, candle_close=99.8, candle_high=100.1, candle_low=99.6,
                    oi_change_15m_pct=0.10, adx14=15.0,
                ))
                opened = lab.ledger.open_rows(lab.GRID)
                self.assertEqual(len(opened), 1)
                target_distance_pct = abs(opened[0]["target_price"] - opened[0]["entry_price"]) / opened[0]["entry_price"] * 100
                self.assertGreater(target_distance_pct, 0.25)
            finally:
                lab.close()

    def test_trend_opens_and_closes_without_broker(self):
        with tempfile.TemporaryDirectory() as folder:
            lab = ParallelStrategyLab({"enabled": True, "trend": {}, "grid": {}}, Path(folder) / "lab.sqlite")
            try:
                lab.process(market())
                self.assertEqual(len(lab.ledger.open_rows(lab.TREND)), 1)
                lab.process(market(timestamp_ms=1_300_000, candle_open_time_ms=1_200_000,
                                   candle_high=101.0, candle_low=99.0, candle_close=99.5, price=99.5))
                self.assertEqual(len(lab.ledger.open_rows(lab.TREND)), 0)
                self.assertEqual(lab.ledger.summary()[lab.TREND]["closed"], 1)
            finally:
                lab.close()

    def test_ledger_applies_round_trip_costs(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = SimulationLedger(Path(folder) / "lab.sqlite", 0.05, 0.02)
            snapshot = market()
            self.assertTrue(ledger.open_trade("X", snapshot, "LONG", 100.0, 99.0, 102.0, {}))
            row = ledger.open_rows("X")[0]
            ledger.close_trade(row, 102.0, 2_000_000, "TARGET")
            closed = ledger.recent(1)[0]
            self.assertLess(closed["net_pct"], 2.0)
            self.assertGreater(closed["net_pct"], 1.7)
            ledger.close()

    def test_dump_logical_exit_runs_past_old_target_then_trails_peak(self):
        with tempfile.TemporaryDirectory() as folder:
            lab = ParallelStrategyLab({
                "enabled": True,
                "logical_exit": {
                    "enabled": True, "min_giveback_pct": 0.65,
                    "max_giveback_pct": 1.4, "atr_giveback_multiple": 1.25,
                },
                "trend": {"enabled": False}, "grid": {"enabled": False},
            }, Path(folder) / "lab.sqlite")
            try:
                signal = {
                    "setup": "DUMP_EXHAUSTION_RECLAIM_V1", "symbol": "TESTUSDT",
                    "direction": "LONG", "timestamp_ms": 1_000_000,
                    "entry_price": 100.0, "stop_price": 98.5, "target_price": 103.0,
                    "risk_pct": 1.5,
                    "evidence": {},
                }
                self.assertTrue(lab.open_native_target_runner(signal, market(price=100.0)))
                lab.process(market(timestamp_ms=1_300_000, candle_open_time_ms=1_200_000,
                                   price=103.2, candle_high=103.4, candle_low=102.7,
                                   candle_close=103.2, atr14_pct=0.5))
                self.assertEqual(len(lab.ledger.open_rows(lab.DUMP_LOGICAL)), 1)
                lab.process(market(timestamp_ms=1_600_000, candle_open_time_ms=1_500_000,
                                   price=105.0, candle_high=105.2, candle_low=104.7,
                                   candle_close=105.0, atr14_pct=0.5))
                self.assertEqual(len(lab.ledger.open_rows(lab.DUMP_LOGICAL)), 1)
                lab.process(market(timestamp_ms=1_900_000, candle_open_time_ms=1_800_000,
                                   price=104.3, candle_high=105.0, candle_low=104.2,
                                   candle_close=104.3, atr14_pct=0.5))
                closed = lab.ledger.recent(1)[0]
                self.assertEqual(closed["exit_reason"], "LOGICAL_TRAIL")
                self.assertGreater(closed["net_pct"], 4.0)
            finally:
                lab.close()

    def test_native_runner_uses_each_setups_own_target_as_floor(self):
        with tempfile.TemporaryDirectory() as folder:
            lab = ParallelStrategyLab({
                "enabled": True,
                "logical_exit": {"enabled": True, "min_giveback_pct": 0.65,
                                 "max_giveback_pct": 1.4, "atr_giveback_multiple": 1.25},
                "trend": {"enabled": False}, "grid": {"enabled": False},
            }, Path(folder) / "lab.sqlite")
            try:
                signal = {
                    "setup": "BORDER_BREAKOUT_RETEST_LONG", "symbol": "TESTUSDT",
                    "direction": "LONG", "timestamp_ms": 1_000_000,
                    "entry_price": 100.0, "stop_price": 99.35, "target_price": 101.43,
                    "risk_pct": 0.65, "evidence": {},
                }
                self.assertTrue(lab.open_native_target_runner(signal, market(price=100.0)))
                lab.process(market(timestamp_ms=1_300_000, candle_open_time_ms=1_200_000,
                                   price=102.2, candle_high=102.4, candle_low=101.8,
                                   candle_close=102.2, atr14_pct=0.4))
                lab.process(market(timestamp_ms=1_600_000, candle_open_time_ms=1_500_000,
                                   price=101.6, candle_high=102.2, candle_low=101.5,
                                   candle_close=101.6, atr14_pct=0.4))
                closed = lab.ledger.recent(1)[0]
                self.assertEqual(closed["exit_reason"], "LOGICAL_TRAIL")
                self.assertGreaterEqual(closed["gross_pct"], 1.43 - 1e-6)
                self.assertEqual(closed["evidence"]["source_setup"], "BORDER_BREAKOUT_RETEST_LONG")
            finally:
                lab.close()

    def test_native_runner_ignores_signal_candles_pre_entry_ohlc(self):
        with tempfile.TemporaryDirectory() as folder:
            lab = ParallelStrategyLab({
                "enabled": True,
                "logical_exit": {"enabled": True, "min_giveback_pct": 0.65,
                                 "max_giveback_pct": 1.4, "atr_giveback_multiple": 1.25},
                "trend": {"enabled": False}, "grid": {"enabled": False},
            }, Path(folder) / "lab.sqlite")
            try:
                opening = market(timestamp_ms=1_000_000, candle_open_time_ms=900_000, price=100.0)
                signal = {
                    "setup": "DUMP_EXHAUSTION_RECLAIM_V1", "symbol": "TESTUSDT",
                    "direction": "LONG", "timestamp_ms": 1_000_000,
                    "entry_price": 100.0, "stop_price": 98.5, "target_price": 103.0,
                    "risk_pct": 1.5, "evidence": {},
                }
                self.assertTrue(lab.open_native_target_runner(signal, opening))
                lab.process(market(
                    timestamp_ms=1_100_000, candle_open_time_ms=900_000,
                    price=100.1, candle_low=97.0, candle_high=101.0,
                ))
                self.assertEqual(len(lab.ledger.open_rows(lab.DUMP_LOGICAL)), 1)
                lab.process(market(
                    timestamp_ms=1_300_000, candle_open_time_ms=1_200_000,
                    price=98.0, candle_low=97.9, candle_high=100.2,
                ))
                self.assertEqual(len(lab.ledger.open_rows(lab.DUMP_LOGICAL)), 0)
            finally:
                lab.close()

    def test_leveraged_half_geometry_halves_tp_sl_and_reports_50x20_pnl(self):
        with tempfile.TemporaryDirectory() as folder:
            lab = ParallelStrategyLab({
                "enabled": True, "fee_pct_per_side": 0.05, "slippage_pct_per_side": 0.0,
                "logical_exit": {"enabled": True, "min_giveback_pct": 0.65,
                                 "max_giveback_pct": 1.4, "atr_giveback_multiple": 1.25},
                "leveraged_half_geometry": {
                    "enabled": True, "margin_usdt": 50.0,
                    "leverage": 20, "geometry_scale": 0.5,
                },
                "trend": {"enabled": False}, "grid": {"enabled": False},
            }, Path(folder) / "lab.sqlite")
            try:
                signal = {
                    "setup": "VOLATILITY_EXHAUSTION_FADE_SCALP_V1",
                    "symbol": "TESTUSDT", "direction": "LONG", "timestamp_ms": 1_000_000,
                    "entry_price": 100.0, "stop_price": 98.0, "target_price": 100.8,
                    "risk_pct": 2.0, "evidence": {},
                }
                self.assertFalse(lab.open_leveraged_half_geometry(signal, market(price=100.0)))
                self.assertTrue(lab.open_leveraged_half_geometry(signal, market(price=100.0), {
                    "id": 77, "entry_price": 100.0,
                    "initial_stop_price": 98.0, "target_price": 100.8,
                }))
                row = lab.ledger.open_rows(lab.LEVERAGED_HALF_PREFIX + signal["setup"])[0]
                self.assertAlmostEqual(row["stop_price"], 99.0)
                self.assertAlmostEqual(row["evidence"]["native_target_price"], 100.4)
                self.assertEqual(row["evidence"]["notional_usdt"], 1000.0)
                self.assertEqual(row["evidence"]["source"], "REAL_SHADOW_ACCEPTED_FILL")
                self.assertEqual(row["evidence"]["source_shadow_trade_id"], 77)
                lab.ledger.close_trade(row, 100.4, 1_300_000, "TEST_TARGET")
                closed = lab.ledger.recent(1)[0]
                self.assertAlmostEqual(closed["net_pct"], 0.30)
                self.assertAlmostEqual(closed["estimated_net_pnl_usdt"], 3.0)
                self.assertAlmostEqual(closed["estimated_roe_pct"], 6.0)
            finally:
                lab.close()


if __name__ == "__main__":
    unittest.main()
