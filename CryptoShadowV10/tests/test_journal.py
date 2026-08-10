import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from engine.binance import MarketSnapshot
from engine.journal import TradeJournal
from engine.strategy import StrategyEvent


def snap(price, ts):
    return MarketSnapshot(
        symbol="BTCUSDT",
        timestamp_ms=ts,
        price=price,
        price_change_15m_pct=0,
        oi_change_15m_pct=0,
        global_lsr=1,
        taker_buy_sell_ratio=1,
        funding_rate_pct=0,
        volume_ratio=1,
        spread_pct=0.01,
        prior_high=99,
        prior_low=98,
        candle_high=price,
        candle_low=price,
    )


class JournalTests(unittest.TestCase):
    def test_feature_observations_receive_forward_labels(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = TradeJournal(Path(folder) / "test.sqlite", 0.04)
            snapshot = snap(100.0, 1_000_000)
            journal.record_feature_observation(snapshot, [{"strategy": "TEST", "passed": 3}])
            self.assertEqual(journal.feature_observation_summary()["total"], 1)
            journal.update_feature_outcomes({"BTCUSDT": {"price": 101.0}}, 1_300_000)
            summary = journal.feature_observation_summary()
            self.assertEqual(summary["labeled_5m"], 1)
            self.assertEqual(summary["labeled_15m"], 0)
            journal.close()

    def test_shadow_target_is_closed_net_of_fees(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = TradeJournal(
                Path(folder) / "test.sqlite", fee_pct_per_side=0.04,
                reference_management={"partial_at_r": 1.0, "partial_fraction": 0.4},
            )
            signal = {
                "timestamp_ms": 1000, "symbol": "BTCUSDT", "direction": "LONG", "setup": "OI_BREAKOUT_RETEST",
                "entry_price": 100.0, "stop_price": 99.5, "target_price": 101.0, "risk_pct": 0.5, "evidence": {},
            }
            self.assertEqual(journal.record_signal(signal, 3), (True, "opened_reference"))
            self.assertIsNone(journal.update_position(snap(100.6, 1500)))
            closed = journal.update_position(snap(101.1, 2000))
            self.assertEqual(closed["reason"], "REFERENCE_TARGET")
            self.assertEqual(closed["setup"], "OI_BREAKOUT_RETEST")
            self.assertTrue(closed["partial_taken"])
            self.assertAlmostEqual(closed["net_pct"], 0.72)
            self.assertAlmostEqual(closed["result_r"], 1.44)
            journal.close()

    def test_concurrent_limit_is_hard(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = TradeJournal(Path(folder) / "test.sqlite", 0.04)
            signal = {"timestamp_ms": 1, "symbol": "BTCUSDT", "direction": "LONG", "setup": "OI_BREAKOUT_RETEST", "entry_price": 100, "stop_price": 99, "target_price": 102, "risk_pct": 1, "evidence": {}}
            self.assertTrue(journal.record_signal(signal, 1)[0])
            second = dict(signal, symbol="ETHUSDT")
            self.assertEqual(journal.record_signal(second, 1), (False, "max_concurrent_shadow_positions"))
            journal.close()

    def test_active_setup_and_cooldown_survive_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = TradeJournal(Path(folder) / "test.sqlite", 0.04)
            state = {
                "symbol": "BTCUSDT", "direction": "LONG", "armed_at_ms": 1000,
                "expires_at_ms": 100_000, "breakout_price": 100, "favorable_extreme": 101,
                "pullback_extreme": None, "phase": "ARMED", "arm_snapshot": {},
            }
            journal.record_event(StrategyEvent("ARMED", "BTCUSDT", "LONG", 1000, "test", {"state": state}))
            active, cooldowns = journal.restored_strategy_state(2000)
            self.assertEqual(active[0]["symbol"], "BTCUSDT")
            self.assertEqual(cooldowns, {})
            journal.record_event(StrategyEvent("SIGNAL", "BTCUSDT", "LONG", 3000, "test", {}))
            active, cooldowns = journal.restored_strategy_state(4000)
            self.assertEqual(active, [])
            self.assertEqual(cooldowns["BTCUSDT"], 3000)
            journal.close()

    def test_concurrent_dashboard_reads_do_not_corrupt_sqlite_cursor(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = TradeJournal(Path(folder) / "test.sqlite", 0.05)
            event = StrategyEvent("CAMPAIGN_HOLD", "BTCUSDT", "LONG", 1000, "test", {})

            def writer():
                for _ in range(100):
                    journal.record_event(event)

            def reader():
                for _ in range(100):
                    journal.recent_events(20)
                    journal.recent_campaign_actions(20)
                    journal.pending_executions()
                    journal.execution_ledger()

            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(writer), pool.submit(reader), pool.submit(reader), pool.submit(reader)]
                for future in futures:
                    future.result()
            self.assertEqual(len(journal.recent_campaign_actions(100)), 100)
            journal.close()


if __name__ == "__main__":
    unittest.main()
