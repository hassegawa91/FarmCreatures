import tempfile
import unittest
from pathlib import Path

from engine.campaign import CampaignPolicy
from engine.journal import TradeJournal
from engine.real_shadow import RealMarketShadow
from engine.simulation import SimulationLedger
from engine.strategy import StrategyEvent


class LedgerResetTests(unittest.TestCase):
    def test_testnet_reset_clears_journal_tables(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = TradeJournal(Path(folder) / "testnet.sqlite", 0.05)
            journal.record_event(StrategyEvent("ARMED", "BTCUSDT", "LONG", 1, "test", {}))
            counts = journal.reset()
            self.assertEqual(counts["events"], 1)
            self.assertEqual(journal.recent_events(10), [])
            journal.close()

    def test_shadow_reset_clears_events(self):
        with tempfile.TemporaryDirectory() as folder:
            shadow = RealMarketShadow(
                {}, {}, CampaignPolicy({}), object(), Path(folder) / "shadow.sqlite",
            )
            shadow._event(1, "BTCUSDT", "TEST", "OPEN", "test")
            counts = shadow.reset()
            self.assertEqual(counts["real_shadow_events"], 1)
            self.assertEqual(shadow.status()["recent_trades"], [])
            shadow.close()

    def test_simulation_reset_clears_events(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = SimulationLedger(Path(folder) / "sim.sqlite", 0.05, 0.02)
            ledger.event(1, "TEST", "BTCUSDT", "OPEN", {})
            counts = ledger.reset()
            self.assertEqual(counts["simulation_events"], 1)
            self.assertEqual(ledger.recent(10), [])
            ledger.close()


if __name__ == "__main__":
    unittest.main()
