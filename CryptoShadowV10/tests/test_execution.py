import os
import tempfile
import unittest
from pathlib import Path

from engine.execution import BinanceFuturesBroker, ExecutionRouter, RiskSizer


class FakeBroker(BinanceFuturesBroker):
    def __init__(self, fail_take=False):
        self.mode = "TESTNET"
        self.settings = {
            "leverage": 5, "account_risk_pct": 0.25, "max_margin_pct": 5,
            "max_open_positions": 3, "max_entry_drift_pct": 0.15,
            "margin_type": "ISOLATED", "working_type": "MARK_PRICE",
        }
        self.fail_take = fail_take
        self.ask_price = 100.0
        self.bid_price = 99.99
        self.fill_price = 100.0
        self.calls = []
        self.rollback = []

    def request(self, method, path, params=None, signed=True):
        self.calls.append((method, path, params))
        if path == "/fapi/v1/positionSide/dual":
            return {"dualSidePosition": False}
        if path == "/fapi/v1/ticker/bookTicker":
            return {"askPrice": str(self.ask_price), "bidPrice": str(self.bid_price)}
        if path == "/fapi/v1/depth":
            return getattr(self, "depth", {
                "asks": [[str(self.ask_price), "1000"]],
                "bids": [[str(self.bid_price), "1000"]],
            })
        if path == "/fapi/v1/order":
            return {"orderId": 1, "avgPrice": str(self.fill_price), "status": "FILLED"}
        if method == "POST" and path == "/fapi/v1/algoOrder" and params["type"] == "STOP_MARKET":
            return {"algoId": 2}
        if method == "POST" and path == "/fapi/v1/algoOrder" and self.fail_take:
            raise RuntimeError("take failed")
        if method == "POST" and path == "/fapi/v1/algoOrder":
            return {"algoId": 3}
        return {"ok": True}

    def account_state(self):
        return {"mode": "TESTNET", "positions": [], "orders": [], "algo_orders": []}

    def account(self):
        return {"totalWalletBalance": "1000"}

    def symbol_rules(self, symbol):
        return {"step_size": 0.001, "min_qty": 0.001, "max_qty": 1000, "tick_size": 0.1, "min_notional": 5}

    def _configure_symbol(self, symbol):
        return None

    def _market_close(self, symbol, side, quantity):
        self.rollback.append((symbol, side, quantity))
        return {"orderId": 9, "status": "FILLED"}

    def _actual_position_entry(self, symbol, direction, fallback):
        return fallback, "ORDER_RESULT"


class ExecutionTests(unittest.TestCase):
    SIGNAL = {
        "timestamp_ms": 1, "symbol": "BTCUSDT", "direction": "LONG", "setup": "OI_BREAKOUT_RETEST",
        "entry_price": 100.0, "stop_price": 99.0, "target_price": 102.0, "risk_pct": 1.0, "evidence": {},
    }
    def test_risk_sizing_uses_tighter_of_risk_and_margin_caps(self):
        qty = RiskSizer.quantity(
            wallet_balance=1000, entry_price=100, stop_price=99,
            leverage=5, account_risk_pct=0.25, max_margin_pct=5,
        )
        self.assertEqual(qty, 2.5)  # risk cap: 2.5 USDT / 1 USDT distance
        margin_limited = RiskSizer.quantity(1000, 100, 99.9, 5, 1.0, 1.0)
        self.assertEqual(margin_limited, 0.5)

    def test_risk_sizing_reserves_round_trip_commission(self):
        qty = RiskSizer.quantity(
            wallet_balance=1000, entry_price=100, stop_price=99,
            leverage=5, account_risk_pct=0.25, max_margin_pct=5,
            round_trip_fee_rate=0.001,
        )
        self.assertAlmostEqual(qty, 2.2727272727)

    def test_fixed_margin_uses_requested_margin_and_leverage(self):
        qty = RiskSizer.quantity(
            wallet_balance=5000, entry_price=100, stop_price=98.8,
            leverage=10, account_risk_pct=0.03, max_margin_pct=3,
            round_trip_fee_rate=0.001, fixed_margin_usdt=50,
        )
        self.assertEqual(qty, 5.0)  # 50 USDT de margem x 10 = 500 USDT notional

    def test_fixed_margin_execution_rejects_exchange_quantity_cap_instead_of_downsizing(self):
        broker = FakeBroker()
        broker.settings.update({"fixed_margin_usdt": 50.0, "fixed_margin_tolerance_pct": 2.0})
        broker.symbol_rules = lambda symbol: {
            "step_size": 0.001, "min_qty": 0.001, "max_qty": 1.0,
            "tick_size": 0.1, "min_notional": 5,
        }
        with self.assertRaisesRegex(RuntimeError, "simbolo nao comporta margem padrao"):
            broker.execute(dict(self.SIGNAL))
        self.assertFalse(any(method == "POST" and path == "/fapi/v1/order" for method, path, _ in broker.calls))

    def test_fixed_margin_execution_reports_effective_standard_margin(self):
        broker = FakeBroker()
        broker.settings.update({"fixed_margin_usdt": 50.0, "fixed_margin_tolerance_pct": 2.0})
        result = broker.execute(dict(self.SIGNAL))
        self.assertAlmostEqual(result["effective_entry_margin_usdt"], 50.0)
        self.assertEqual(result["leverage"], 5)

    def test_quantity_rounds_down_to_exchange_step(self):
        self.assertEqual(str(RiskSizer.floor_step(1.239, 0.01)), "1.23")

    def test_trigger_rounding_is_directionally_explicit(self):
        self.assertEqual(str(RiskSizer.trigger_price(10.006, 0.01, False)), "10.00")
        self.assertEqual(str(RiskSizer.trigger_price(10.006, 0.01, True)), "10.01")

    def test_real_broker_fails_closed_without_external_confirmation(self):
        settings = {
            "enabled": True, "real_base_url": "https://fapi.binance.com", "testnet_base_url": "x",
            "real_confirmation_env": "V10_TEST_REAL_CONFIRM", "real_confirmation_value": "ENABLE_REAL_TRADING",
        }
        os.environ.pop("V10_TEST_REAL_CONFIRM", None)
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(RuntimeError, "confirmacao externa"):
                BinanceFuturesBroker("REAL", settings, Path(folder))

    def test_disabled_execution_fails_for_testnet_and_real(self):
        settings = {"enabled": False, "testnet_base_url": "x", "real_base_url": "x"}
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(RuntimeError, "execution.enabled=false"):
                BinanceFuturesBroker("TESTNET", settings, Path(folder))

    def test_entry_pause_keeps_router_alive_but_blocks_new_orders(self):
        router = ExecutionRouter.__new__(ExecutionRouter)
        router.mode = "TESTNET"
        router.settings = {"new_entries_enabled": False}
        router.broker = object()
        result = router.execute(dict(self.SIGNAL), journal=None, max_concurrent_shadow=3)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "novas_entradas_pausadas")

    def test_protected_execution_places_stop_before_take(self):
        broker = FakeBroker()
        result = broker.execute(dict(self.SIGNAL))
        self.assertTrue(result["ok"])
        algo_types = [params["type"] for method, path, params in broker.calls if path == "/fapi/v1/algoOrder" and method == "POST"]
        self.assertEqual(algo_types, ["STOP_MARKET", "TAKE_PROFIT_MARKET"])
        self.assertEqual(broker.rollback, [])
        self.assertAlmostEqual(result["taker_commission_rate"], 0.0005)
        self.assertGreater(result["fill_net_rr"], 1.5)
        self.assertEqual(result["initial_stop_price"], result["stop_price"])
        self.assertGreater(result["initial_risk_quote"], 0)

    def test_strategy_can_override_protection_working_type(self):
        broker = FakeBroker()
        broker.settings["strategy_working_type_overrides"] = {
            "VOLATILITY_EXHAUSTION_FADE_SCALP_V1": "CONTRACT_PRICE",
        }
        signal = {**self.SIGNAL, "setup": "VOLATILITY_EXHAUSTION_FADE_SCALP_V1"}
        result = broker.execute(signal)
        protection_calls = [
            params for method, path, params in broker.calls
            if method == "POST" and path == "/fapi/v1/algoOrder"
        ]
        self.assertEqual([call["workingType"] for call in protection_calls], [
            "CONTRACT_PRICE", "CONTRACT_PRICE",
        ])
        self.assertEqual(result["working_type"], "CONTRACT_PRICE")

    def test_full_position_runner_starts_with_stop_but_without_fixed_take(self):
        broker = FakeBroker()
        signal = {**self.SIGNAL, "full_position_runner": True}
        result = broker.execute(signal)
        self.assertTrue(result["ok"])
        algo_types = [
            params["type"] for method, path, params in broker.calls
            if path == "/fapi/v1/algoOrder" and method == "POST"
        ]
        self.assertEqual(algo_types, ["STOP_MARKET"])
        self.assertTrue(result["runner_target_disabled"])
        self.assertIsNone(result["take_algo_id"])

    def test_actual_position_entry_overrides_unreliable_order_average(self):
        broker = FakeBroker()
        broker.positions = lambda: [{
            "symbol": "BTCUSDT", "positionAmt": "2.5", "entryPrice": "100.73",
        }]
        actual, source = BinanceFuturesBroker._actual_position_entry(broker, "BTCUSDT", "LONG", 100.0)
        self.assertEqual(actual, 100.73)
        self.assertEqual(source, "POSITION_RISK")

    def test_execution_rejects_trade_whose_fees_destroy_minimum_rr(self):
        broker = FakeBroker()
        broker.settings["min_executable_rr"] = 1.5
        signal = dict(self.SIGNAL, stop_price=99.7, target_price=100.66)
        with self.assertRaisesRegex(RuntimeError, "RR liquido executavel insuficiente"):
            broker.execute(signal)
        self.assertFalse(any(method == "POST" and path == "/fapi/v1/order" for method, path, _ in broker.calls))

    def test_execution_rejects_adverse_drift_and_degraded_rr_before_opening(self):
        broker = FakeBroker()
        broker.settings["max_entry_drift_pct"] = 0.05
        broker.ask_price = 100.06
        with self.assertRaisesRegex(RuntimeError, "drift adverso"):
            broker.execute(dict(self.SIGNAL))
        self.assertFalse(any(method == "POST" and path == "/fapi/v1/order" for method, path, _ in broker.calls))

    def test_execution_rejects_pathological_venue_spread_before_opening(self):
        broker = FakeBroker()
        broker.settings["max_execution_spread_pct"] = 0.15
        broker.ask_price = 100.20
        broker.bid_price = 99.80
        with self.assertRaisesRegex(RuntimeError, "spread executavel excessivo"):
            broker.execute(dict(self.SIGNAL))
        self.assertFalse(any(method == "POST" and path == "/fapi/v1/order" for method, path, _ in broker.calls))

    def test_testnet_can_use_a_separate_entry_spread_limit(self):
        broker = FakeBroker()
        broker.settings.update({
            "max_entry_execution_spread_pct": 0.25,
            "testnet_max_entry_execution_spread_pct": 0.30,
            "max_entry_drift_pct": 0.20,
        })
        broker.ask_price, broker.bid_price = 100.14, 99.87
        signal = dict(self.SIGNAL, target_price=102.50)
        self.assertTrue(broker.execute(signal)["ok"])

        broker = FakeBroker()
        broker.mode = "REAL"
        broker.settings.update({
            "max_entry_execution_spread_pct": 0.25,
            "testnet_max_entry_execution_spread_pct": 0.30,
            "max_entry_drift_pct": 0.20,
        })
        broker.ask_price, broker.bid_price = 100.14, 99.87
        with self.assertRaisesRegex(RuntimeError, "spread executavel excessivo"):
            broker.execute(signal)

    def test_depth_filter_rejects_market_order_with_excessive_book_impact(self):
        broker = FakeBroker()
        broker.settings["depth_filter"] = {
            "enabled": True, "levels": 20,
            "max_entry_impact_pct": 0.20,
            "testnet_max_entry_impact_pct": 0.20,
        }
        broker.depth = {
            "asks": [["100.00", "1.0"], ["101.00", "1000"]],
            "bids": [["99.99", "1000"]],
        }
        with self.assertRaisesRegex(RuntimeError, "impacto de profundidade excessivo"):
            broker.execute(dict(self.SIGNAL))
        self.assertFalse(any(method == "POST" and path == "/fapi/v1/order" for method, path, _ in broker.calls))

    def test_testnet_uses_real_market_admission_instead_of_demo_liquidity(self):
        broker = FakeBroker()
        broker.settings.update({
            "testnet_require_real_market_admission": True,
            "testnet_rebase_market_basis": True,
            "testnet_max_market_basis_pct": 5.0,
            "testnet_max_entry_execution_spread_pct": 0.01,
            "depth_filter": {
                "enabled": True, "levels": 20,
                "max_entry_impact_pct": 0.01,
                "testnet_max_entry_impact_pct": 0.01,
            },
        })
        broker.ask_price, broker.bid_price = 100.20, 99.80
        result = broker.execute(dict(self.SIGNAL))
        self.assertTrue(result["ok"])
        self.assertFalse(any(path == "/fapi/v1/depth" for _, path, _ in broker.calls))

    def test_standard_margin_symbols_excludes_exchange_quantity_cap(self):
        broker = FakeBroker()
        broker.settings.update({"fixed_margin_usdt": 50.0, "fixed_margin_tolerance_pct": 2.0})
        broker.supported_symbols = lambda: {"OKUSDT", "CAPPEDUSDT"}
        broker.request = lambda method, path, params=None, signed=True: [
            {"symbol": "OKUSDT", "markPrice": "100"},
            {"symbol": "CAPPEDUSDT", "markPrice": "100"},
        ]
        broker.symbol_rules = lambda symbol: {
            "step_size": 0.001, "min_qty": 0.001,
            "max_qty": 1000.0 if symbol == "OKUSDT" else 0.05,
            "tick_size": 0.1, "min_notional": 5,
        }
        self.assertEqual(broker.standard_margin_symbols(), {"OKUSDT"})

    def test_testnet_basis_rebases_levels_instead_of_rejecting_valid_signal(self):
        broker = FakeBroker()
        broker.settings["testnet_rebase_market_basis"] = True
        broker.settings["testnet_max_market_basis_pct"] = 0.5
        broker.ask_price = 100.23
        broker.bid_price = 100.22
        broker.fill_price = 100.23
        result = broker.execute(dict(self.SIGNAL))
        self.assertTrue(result["testnet_levels_rebased"])
        self.assertAlmostEqual(result["entry_drift_pct"], 0.23)
        self.assertEqual(result["stop_price"], 99.2)
        self.assertEqual(result["target_price"], 102.3)

        blocked = FakeBroker()
        blocked.settings["testnet_rebase_market_basis"] = True
        blocked.settings["testnet_max_market_basis_pct"] = 0.5
        blocked.ask_price = 100.6
        blocked.bid_price = 100.59
        with self.assertRaisesRegex(RuntimeError, "basis Testnet excessiva"):
            blocked.execute(dict(self.SIGNAL))

    def test_execution_rolls_back_when_market_fill_degrades_rr(self):
        broker = FakeBroker()
        broker.settings["min_executable_rr"] = 1.5
        broker.fill_price = 100.4
        result = broker.execute(dict(self.SIGNAL))
        self.assertTrue(result["safety_closed"])
        self.assertEqual(result["reason"], "fill_degraded_net_rr")
        self.assertEqual(broker.rollback, [("BTCUSDT", "SELL", "2.272")])

        broker = FakeBroker()
        broker.settings["min_executable_rr"] = 1.5
        broker.ask_price = 100.4
        broker.bid_price = 100.39
        broker.settings["max_entry_drift_pct"] = 1.0
        with self.assertRaisesRegex(RuntimeError, "RR liquido executavel insuficiente"):
            broker.execute(dict(self.SIGNAL))
        self.assertFalse(any(method == "POST" and path == "/fapi/v1/order" for method, path, _ in broker.calls))

    def test_take_failure_cancels_stop_and_rolls_back_position(self):
        broker = FakeBroker(fail_take=True)
        result = broker.execute(dict(self.SIGNAL))
        self.assertTrue(result["safety_closed"])
        self.assertEqual(result["reason"], "protection_rejected")
        self.assertIn("take failed", result["error"])
        self.assertEqual(broker.rollback, [("BTCUSDT", "SELL", "2.272")])
        self.assertTrue(any(method == "DELETE" and path == "/fapi/v1/algoOrder" for method, path, _ in broker.calls))

    def test_campaign_close_symbol_is_reduce_only_and_cancels_protection(self):
        broker = FakeBroker()
        broker.account_state = lambda monitored=None: {
            "mode": "TESTNET", "positions": [{"symbol": "BTCUSDT", "positionAmt": "2.5"}],
            "orders": [], "algo_orders": [{"symbol": "BTCUSDT", "algoId": 77}],
        }
        broker.open_algo_orders = lambda symbol=None: [{"symbol": symbol, "algoId": 77}]
        result = broker.close_symbol("BTCUSDT")
        self.assertTrue(result["closed"])
        self.assertEqual(broker.rollback, [("BTCUSDT", "SELL", "2.5")])
        self.assertTrue(any(method == "DELETE" and path == "/fapi/v1/algoOrder" for method, path, _ in broker.calls))

    def test_campaign_partial_reprotects_remaining_position(self):
        broker = FakeBroker()
        broker.account_state = lambda monitored=None: {
            "mode": "TESTNET", "positions": [{
                "symbol": "BTCUSDT", "positionAmt": "2.0", "markPrice": "100.5",
            }],
            "orders": [], "algo_orders": [],
        }
        broker.open_algo_orders = lambda symbol=None: []
        result = broker.take_partial("BTCUSDT", "LONG", 0.5, 100.1, 102.0, 99.0)
        self.assertEqual(result["quantity_closed"], "1.0")
        self.assertEqual(broker.rollback, [("BTCUSDT", "SELL", "1.0")])
        self.assertFalse(result["runner_stop_adjusted"])
        algo_types = [params["type"] for method, path, params in broker.calls if path == "/fapi/v1/algoOrder" and method == "POST"]
        self.assertEqual(algo_types, ["STOP_MARKET", "TAKE_PROFIT_MARKET"])

    def test_campaign_partial_is_deferred_without_canceling_protection_when_spread_is_wide(self):
        broker = FakeBroker()
        broker.settings["max_execution_spread_pct"] = 0.15
        broker.ask_price = 100.20
        broker.bid_price = 99.80
        result = broker.take_partial("BTCUSDT", "LONG", 0.5, 100.1, 102.0, 99.0)
        self.assertTrue(result["deferred"])
        self.assertEqual(result["reason"], "execution_spread_too_wide")
        self.assertEqual(broker.rollback, [])
        self.assertFalse(any(method == "DELETE" for method, _, _ in broker.calls))

    def test_campaign_partial_uses_original_stop_if_runner_stop_is_already_crossed(self):
        broker = FakeBroker()
        broker.account_state = lambda monitored=None: {
            "mode": "TESTNET", "positions": [{
                "symbol": "BTCUSDT", "positionAmt": "-2.0", "markPrice": "100.5",
            }],
            "orders": [], "algo_orders": [],
        }
        broker.open_algo_orders = lambda symbol=None: []
        result = broker.take_partial("BTCUSDT", "SHORT", 0.5, 100.2, 98.0, 101.0)
        self.assertTrue(result["runner_stop_adjusted"])
        self.assertEqual(result["requested_runner_stop_price"], 100.2)
        self.assertEqual(result["stop_price"], 101.0)

    def test_profit_stop_replaces_old_stop_in_binance_supported_order(self):
        broker = FakeBroker()
        broker.account_state = lambda monitored=None: {
            "mode": "TESTNET", "positions": [{
                "symbol": "BTCUSDT", "positionAmt": "2.0", "markPrice": "101.0",
            }],
            "orders": [], "algo_orders": [],
        }
        broker.open_algo_orders = lambda symbol=None: [{
            "symbol": symbol, "algoId": 77, "orderType": "STOP_MARKET", "triggerPrice": "99.0",
        }, {
            "symbol": symbol, "algoId": 88, "orderType": "TAKE_PROFIT_MARKET",
        }]
        result = broker.update_stop("BTCUSDT", "LONG", 100.3)
        self.assertEqual(result["stop_price"], 100.3)
        post_index = next(i for i, call in enumerate(broker.calls) if call[0] == "POST" and call[1] == "/fapi/v1/algoOrder")
        delete_index = next(i for i, call in enumerate(broker.calls) if call[0] == "DELETE" and call[1] == "/fapi/v1/algoOrder")
        self.assertLess(delete_index, post_index)
        self.assertTrue(any(call[2].get("algoId") == 77 for call in broker.calls if call[0] == "DELETE"))
        self.assertFalse(any(call[2].get("algoId") == 88 for call in broker.calls if call[0] == "DELETE"))

    def test_runner_stop_keeps_contract_price_trigger_basis(self):
        broker = FakeBroker()
        broker.account_state = lambda monitored=None: {
            "mode": "TESTNET", "positions": [{
                "symbol": "BTCUSDT", "positionAmt": "2.0", "markPrice": "101.5",
            }],
            "orders": [], "algo_orders": [],
        }
        broker.contract_price = lambda symbol: 101.0
        broker.open_algo_orders = lambda symbol=None: [{
            "symbol": symbol, "algoId": 77, "orderType": "STOP_MARKET", "triggerPrice": "99.0",
        }]
        result = broker.update_stop("BTCUSDT", "LONG", 100.6, "CONTRACT_PRICE")
        replacement = next(
            params for method, path, params in broker.calls
            if method == "POST" and path == "/fapi/v1/algoOrder"
        )
        self.assertEqual(replacement["workingType"], "CONTRACT_PRICE")
        self.assertEqual(result["working_type"], "CONTRACT_PRICE")

    def test_reconcile_uses_actual_fills_fees_and_cancels_sibling(self):
        broker = FakeBroker()
        broker.user_trades = lambda symbol, start: [
            {"orderId": 1, "time": 1000, "side": "BUY", "qty": "2.5", "price": "100", "commission": "0.1", "realizedPnl": "0"},
            {"orderId": 4, "time": 2000, "side": "SELL", "qty": "2.5", "price": "102", "commission": "0.1", "realizedPnl": "5"},
        ]
        pending = [{
            "id": 7, "timestamp_ms": 1, "mode": "TESTNET", "symbol": "BTCUSDT", "direction": "LONG",
            "signal": dict(self.SIGNAL), "execution": {"open_order_id": 1},
        }]
        state = broker.account_state()
        state["algo_orders"] = [{"symbol": "BTCUSDT", "algoId": 3, "orderType": "TAKE_PROFIT_MARKET"}]
        results = broker.reconcile(pending, state)
        self.assertEqual(len(results), 1)
        self.assertAlmostEqual(results[0]["net_pnl"], 4.8)
        self.assertAlmostEqual(results[0]["result_r"], 1.92)
        self.assertEqual(results[0]["exit_reason"], "TARGET")
        self.assertTrue(any(
            method == "DELETE" and path == "/fapi/v1/algoOrder" and params == {"symbol": "BTCUSDT", "algoId": 3}
            for method, path, params in broker.calls
        ))

    def test_live_account_state_scopes_order_queries_to_monitored_symbols(self):
        broker = FakeBroker()
        broker.positions = lambda: [{"symbol": "ETHUSDT", "positionAmt": "1"}]
        requested = []
        broker.open_orders = lambda symbol=None: requested.append(("normal", symbol)) or []
        broker.open_algo_orders = lambda symbol=None: requested.append(("algo", symbol)) or []
        state = BinanceFuturesBroker.account_state(broker, {"BTCUSDT"})
        self.assertEqual([row["symbol"] for row in state["positions"]], ["ETHUSDT"])
        self.assertEqual(requested, [
            ("normal", "BTCUSDT"), ("algo", "BTCUSDT"),
            ("normal", "ETHUSDT"), ("algo", "ETHUSDT"),
        ])


if __name__ == "__main__":
    unittest.main()
