import tempfile
import unittest
from pathlib import Path

from engine.binance import MarketSnapshot
from engine.campaign import CampaignPolicy
from engine.real_shadow import RealMarketShadow


class FakePublicClient:
    def __init__(self):
        self.price = 100.0

    def get(self, path, params=None):
        if path == "/fapi/v1/depth":
            return {
                "bids": [[str(self.price - 0.01), "1000"]],
                "asks": [[str(self.price + 0.01), "1000"]],
            }
        if path == "/fapi/v1/ticker/bookTicker":
            return [{
                "symbol": "TESTUSDT", "bidPrice": str(self.price - 0.01),
                "askPrice": str(self.price + 0.01),
            }]
        if path == "/fapi/v1/premiumIndex":
            return [{"symbol": "TESTUSDT", "markPrice": str(self.price)}]
        raise AssertionError(path)


def snapshot(price=100.0, **overrides):
    values = dict(
        symbol="TESTUSDT", timestamp_ms=1_000_000, price=price,
        price_change_15m_pct=-1.0, oi_change_15m_pct=0.0, global_lsr=1.0,
        taker_buy_sell_ratio=1.0, funding_rate_pct=0.0, volume_ratio=1.0,
        spread_pct=0.02, prior_high=101.0, prior_low=99.0,
        candle_high=100.2, candle_low=99.0, candle_open=100.0, candle_close=99.5,
        atr14_pct=0.5,
    )
    values.update(overrides)
    return MarketSnapshot(**values)


class RealShadowTests(unittest.TestCase):
    def test_independent_shadow_respects_two_position_limit(self):
        campaign = CampaignPolicy({
            "strategy_roles": {"VOLATILITY_EXHAUSTION_FADE_SCALP_V1": "REVERSAL"},
            "strategy_min_stop_pct_overrides": {"VOLATILITY_EXHAUSTION_FADE_SCALP_V1": 2.0},
            "strategy_max_stop_pct_overrides": {"VOLATILITY_EXHAUSTION_FADE_SCALP_V1": 2.5},
            "strategy_target_r_overrides": {"VOLATILITY_EXHAUSTION_FADE_SCALP_V1": 0.4},
            "preserve_native_target_setups": ["VOLATILITY_EXHAUSTION_FADE_SCALP_V1"],
        })
        client = FakePublicClient()
        settings = {
            "enabled": True, "fixed_margin_usdt": 50, "leverage": 10,
            "fee_pct_per_side": 0.05, "max_open_positions": 2,
            "max_entry_spread_pct": 0.25, "max_entry_impact_pct": 0.20,
            "max_entry_adverse_drift_pct": 0.05, "max_entry_favorable_drift_pct": 0.20,
        }
        execution = {"new_entries_enabled": True, "working_type": "MARK_PRICE", "max_open_positions": 2}
        with tempfile.TemporaryDirectory() as folder:
            shadow = RealMarketShadow(settings, execution, campaign, client, Path(folder) / "limited.sqlite")
            try:
                admissions = []
                for index in range(3):
                    symbol = f"TEST{index}USDT"
                    signal = {
                        "symbol": symbol, "direction": "LONG", "timestamp_ms": 1_000_000 + index,
                        "entry_price": 100.0, "stop_price": 98.0, "target_price": 100.8,
                        "risk_pct": 2.0, "setup": "VOLATILITY_EXHAUSTION_FADE_SCALP_V1",
                        "evidence": {"entry": snapshot(symbol=symbol).to_dict()},
                    }
                    admissions.append(shadow.on_signal(
                        signal, snapshot(symbol=symbol, timestamp_ms=1_000_000 + index),
                    ))
                self.assertEqual(shadow.status()["summary"]["open"], 2)
                self.assertEqual(admissions, [True, True, False])
            finally:
                shadow.close()

    def test_real_prices_manage_independent_full_position_runner(self):
        campaign = CampaignPolicy({
            "strategy_roles": {"VOLATILITY_EXHAUSTION_FADE_SCALP_V1": "REVERSAL"},
            "strategy_min_stop_pct_overrides": {"VOLATILITY_EXHAUSTION_FADE_SCALP_V1": 2.0},
            "strategy_max_stop_pct_overrides": {"VOLATILITY_EXHAUSTION_FADE_SCALP_V1": 2.5},
            "strategy_target_r_overrides": {"VOLATILITY_EXHAUSTION_FADE_SCALP_V1": 0.4},
            "preserve_native_target_setups": ["VOLATILITY_EXHAUSTION_FADE_SCALP_V1"],
            "full_position_runner_setups": ["VOLATILITY_EXHAUSTION_FADE_SCALP_V1"],
            "full_runner_activation_lock_pct_overrides": {"VOLATILITY_EXHAUSTION_FADE_SCALP_V1": 0.60},
            "full_runner_min_giveback_pct_overrides": {"VOLATILITY_EXHAUSTION_FADE_SCALP_V1": 0.40},
            "full_runner_max_giveback_pct_overrides": {"VOLATILITY_EXHAUSTION_FADE_SCALP_V1": 0.80},
            "full_runner_atr_giveback_multiple_overrides": {"VOLATILITY_EXHAUSTION_FADE_SCALP_V1": 0.80},
            "full_runner_structure_disabled_setups": ["VOLATILITY_EXHAUSTION_FADE_SCALP_V1"],
            "runner_min_price_gap_pct_overrides": {"VOLATILITY_EXHAUSTION_FADE_SCALP_V1": 0.10},
        })
        client = FakePublicClient()
        settings = {
            "enabled": True, "fixed_margin_usdt": 50, "leverage": 10,
            "fee_pct_per_side": 0.05, "max_open_positions": 10,
            "max_entry_spread_pct": 0.25, "max_entry_impact_pct": 0.20,
            "max_entry_adverse_drift_pct": 0.05, "max_entry_favorable_drift_pct": 0.20,
        }
        execution = {
            "new_entries_enabled": True, "working_type": "MARK_PRICE",
            "strategy_working_type_overrides": {
                "VOLATILITY_EXHAUSTION_FADE_SCALP_V1": "CONTRACT_PRICE"
            },
        }
        signal = {
            "symbol": "TESTUSDT", "direction": "LONG", "timestamp_ms": 1_000_000,
            "entry_price": 100.0, "stop_price": 98.0, "target_price": 100.8,
            "risk_pct": 2.0, "setup": "VOLATILITY_EXHAUSTION_FADE_SCALP_V1",
            "evidence": {"entry": snapshot().to_dict()},
        }
        with tempfile.TemporaryDirectory() as folder:
            shadow = RealMarketShadow(settings, execution, campaign, client, Path(folder) / "shadow.sqlite")
            shadow.on_signal(signal, snapshot())
            self.assertEqual(shadow.status()["summary"]["open"], 1)
            client.price = 101.0
            shadow.refresh({"TESTUSDT": snapshot(101.0)})
            trade = shadow.status()["recent_trades"][0]
            self.assertEqual(trade["runner_armed"], 1)
            self.assertGreater(trade["current_stop_price"], trade["entry_price"])
            client.price = 100.5
            shadow.refresh({"TESTUSDT": snapshot(100.5)})
            result = shadow.status()
            self.assertEqual(result["summary"]["closed"], 1)
            self.assertGreater(result["summary"]["net_pnl"], 0)
            shadow.close()

    def test_real_shadow_applies_campaign_thesis_exit(self):
        setup = "VOLATILITY_EXHAUSTION_FADE_SCALP_V1"
        campaign = CampaignPolicy({
            "strategy_roles": {setup: "REVERSAL"},
            "strategy_min_stop_pct_overrides": {setup: 2.0},
            "strategy_max_stop_pct_overrides": {setup: 2.5},
            "strategy_target_r_overrides": {setup: 0.4},
            "preserve_native_target_setups": [setup],
            "full_position_runner_setups": [setup],
            "failure_to_launch": {setup: {
                "min_age_minutes": 0, "max_mfe_r": 0.40,
                "max_current_r": -0.20, "min_failures": 2,
            }},
        })
        client = FakePublicClient()
        settings = {
            "enabled": True, "fixed_margin_usdt": 50, "leverage": 10,
            "fee_pct_per_side": 0.05, "max_open_positions": 10,
            "max_entry_spread_pct": 0.25, "max_entry_impact_pct": 0.20,
            "max_entry_adverse_drift_pct": 0.05, "max_entry_favorable_drift_pct": 0.20,
        }
        execution = {"new_entries_enabled": True, "working_type": "MARK_PRICE"}
        signal = {
            "symbol": "TESTUSDT", "direction": "LONG", "timestamp_ms": 1_000_000,
            "entry_price": 100.0, "stop_price": 98.0, "target_price": 100.8,
            "risk_pct": 2.0, "setup": setup,
            "evidence": {"entry": snapshot().to_dict()},
        }
        with tempfile.TemporaryDirectory() as folder:
            shadow = RealMarketShadow(settings, execution, campaign, client, Path(folder) / "shadow.sqlite")
            try:
                shadow.on_signal(signal, snapshot())
                client.price = 99.5
                failed = snapshot(
                    99.5, price_change_15m_pct=-0.5,
                    taker_buy_sell_ratio=0.8, oi_change_5m_pct=0.1,
                )
                shadow.refresh({"TESTUSDT": failed})
                trade = shadow.status()["recent_trades"][0]
                self.assertEqual(trade["status"], "CLOSED")
                self.assertEqual(trade["exit_reason"], "THESIS_EXIT")
            finally:
                shadow.close()


if __name__ == "__main__":
    unittest.main()
