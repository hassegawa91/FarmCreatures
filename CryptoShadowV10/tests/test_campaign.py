import unittest

from engine.campaign import CampaignPolicy


class CampaignPolicyTests(unittest.TestCase):
    SETTINGS = {
        "probe_account_risk_pct": 0.10, "probe_min_stop_pct": 0.55,
        "probe_max_stop_pct": 1.20, "probe_target_r": 2.20,
        "confirm_account_risk_pct": 0.25, "confirm_min_stop_pct": 0.45,
        "confirm_max_stop_pct": 1.20, "confirm_target_r": 2.20,
        "reversal_account_risk_pct": 0.15, "reversal_min_stop_pct": 0.65,
        "reversal_max_stop_pct": 1.40, "reversal_target_r": 2.10,
        "partial_at_r": 0.80, "thesis_review_minutes": 15, "thesis_review_min_mfe_r": 0.35,
        "runner_after_partial_lock_r": 0.10,
        "lock_1_trigger_r": 1.50, "lock_1_profit_r": 0.30,
        "lock_2_trigger_r": 2.00, "lock_2_profit_r": 1.00,
        "thesis_review_max_current_r": 0.0, "thesis_review_min_failures": 2,
        "hard_review_minutes": 60, "hard_review_max_current_r": -0.15,
        "allow_confirm_entry_while_flat": False, "automatic_thesis_flip_enabled": False,
        "max_flips_per_campaign": 1, "signal_flip_min_age_minutes": 5,
    }

    def setUp(self):
        self.policy = CampaignPolicy(self.SETTINGS)

    @staticmethod
    def signal(setup="MICRO_REVERSAL_PROBE", direction="LONG"):
        return {
            "symbol": "BTCUSDT", "direction": direction, "timestamp_ms": 1_000,
            "entry_price": 100.0, "stop_price": 99.7, "target_price": 100.66,
            "risk_pct": 0.3, "setup": setup, "evidence": {},
        }

    def test_probe_widens_stop_but_reduces_account_risk(self):
        decision = self.policy.decide(self.signal(), None)
        self.assertEqual(decision.action, "ENTER")
        self.assertEqual(decision.signal["campaign_action"], "PROBE")
        self.assertAlmostEqual(decision.signal["stop_price"], 99.45)
        self.assertAlmostEqual(decision.signal["target_price"], 101.21)
        self.assertEqual(decision.signal["account_risk_pct"], 0.10)

    def test_strategy_specific_probe_risk_and_target_are_applied(self):
        policy = CampaignPolicy({
            **self.SETTINGS,
            "strategy_roles": {"OI_EXPANSION_ARM_PROBE": "PROBE"},
            "strategy_risk_overrides": {"OI_EXPANSION_ARM_PROBE": 0.03},
            "strategy_target_r_overrides": {"OI_EXPANSION_ARM_PROBE": 2.0},
        })
        decision = policy.decide(self.signal("OI_EXPANSION_ARM_PROBE"), None)
        self.assertEqual(decision.action, "ENTER")
        self.assertEqual(decision.signal["account_risk_pct"], 0.03)
        self.assertAlmostEqual(decision.signal["target_price"], 101.1)

    def test_dump_reversal_preserves_three_percent_native_runner_floor(self):
        policy = CampaignPolicy({
            **self.SETTINGS,
            "allow_confirm_entry_while_flat": True,
            "strategy_roles": {"DUMP_REVERSAL_LONG": "CONFIRM"},
            "strategy_min_stop_pct_overrides": {"DUMP_REVERSAL_LONG": 1.5},
            "strategy_max_stop_pct_overrides": {"DUMP_REVERSAL_LONG": 4.0},
            "strategy_target_r_overrides": {"DUMP_REVERSAL_LONG": 2.0},
            "preserve_native_target_setups": ["DUMP_REVERSAL_LONG"],
        })
        signal = self.signal("DUMP_REVERSAL_LONG")
        signal.update(stop_price=97.0, target_price=103.0, risk_pct=3.0)
        decision = policy.decide(signal, None)
        self.assertEqual(decision.action, "ENTER")
        self.assertAlmostEqual(decision.signal["stop_price"], 97.0)
        self.assertAlmostEqual(decision.signal["target_price"], 103.0)

    def test_confirmation_promotes_probe_in_same_direction(self):
        current = {"signal": {**self.signal(), "campaign_action": "PROBE", "campaign_id": "c1"}}
        decision = self.policy.decide(self.signal("OI_MOMENTUM_PULLBACK"), current)
        self.assertEqual(decision.action, "CONFIRM")
        self.assertEqual(decision.signal["campaign_id"], "c1")
        self.assertEqual(decision.signal["account_risk_pct"], 0.25)

    def test_opposite_reversal_needs_structure_but_probe_does_not_flip(self):
        current = {
            "timestamp_ms": 1_000,
            "signal": {**self.signal(), "campaign_action": "PROBE"},
            "execution": {"current_r": -0.50},
        }
        reversal = self.signal("POST_SQUEEZE_REVERSAL", "SHORT")
        reversal["timestamp_ms"] = 6 * 60_000
        reversal["evidence"] = {"confirmation_checks": {
            "structure_reverses": True, "price_reversal": True, "taker_flips": True,
        }}
        flip = self.policy.decide(reversal, current)
        self.assertEqual(flip.action, "FLIP")
        self.assertEqual(flip.signal["campaign_flip_count"], 1)
        weak = self.policy.decide(self.signal("OI_MOMENTUM_EARLY", "SHORT"), current)
        self.assertEqual(weak.action, "HOLD")

    def test_dump_classifier_can_flip_once_on_structural_opposite_signal(self):
        policy = CampaignPolicy({
            **self.SETTINGS,
            "strategy_roles": {
                "DUMP_REVERSAL_LONG": "REVERSAL",
                "DUMP_CONTINUATION_SHORT": "REVERSAL",
            },
            "strategy_min_stop_pct_overrides": {"DUMP_REVERSAL_LONG": 1.5},
            "strategy_max_stop_pct_overrides": {"DUMP_REVERSAL_LONG": 4.0},
            "strategy_target_r_overrides": {"DUMP_REVERSAL_LONG": 1.75},
            "max_flips_per_campaign": 1,
            "signal_flip_min_age_minutes": 5,
        })
        current = {
            "timestamp_ms": 1_000,
            "signal": {**self.signal("DUMP_CONTINUATION_SHORT", "SHORT"), "campaign_id": "dump-1"},
            "execution": {"current_r": -0.50},
        }
        reversal = self.signal("DUMP_REVERSAL_LONG", "LONG")
        reversal["timestamp_ms"] = 6 * 60_000
        reversal["stop_price"] = 98.5
        reversal["target_price"] = 103.0
        reversal["evidence"] = {"confirmation_checks": {
            "second_structure_reclaim": True,
            "pullback_floor_holds": True,
            "flow_score": True,
        }}
        decision = policy.decide(reversal, current)
        self.assertEqual(decision.action, "FLIP")
        self.assertEqual(decision.signal["campaign_flip_count"], 1)

    def test_confirmation_does_not_open_while_flat(self):
        decision = self.policy.decide(self.signal("OI_MOMENTUM_EARLY"), None)
        self.assertEqual(decision.action, "HOLD")
        self.assertEqual(decision.reason, "confirmation_without_probe")

    def test_confirmation_can_open_while_flat_when_enabled(self):
        policy = CampaignPolicy({**self.SETTINGS, "allow_confirm_entry_while_flat": True})
        decision = policy.decide(self.signal("OI_MOMENTUM_EARLY"), None)
        self.assertEqual(decision.action, "ENTER")
        self.assertEqual(decision.reason, "confirm_while_flat")
        self.assertEqual(decision.signal["campaign_action"], "CONFIRM")
        self.assertEqual(decision.signal["account_risk_pct"], 0.25)

    def test_second_flip_is_blocked(self):
        current = {
            "timestamp_ms": 1_000,
            "signal": {**self.signal(), "campaign_action": "REVERSAL", "campaign_flip_count": 1},
            "execution": {"campaign_flip_count": 1, "current_r": -0.50},
        }
        reversal = self.signal("POST_SQUEEZE_REVERSAL", "SHORT")
        reversal["timestamp_ms"] = 6 * 60_000
        reversal["evidence"] = {"confirmation_checks": {
            "structure_reverses": True, "price_reversal": True, "taker_flips": True,
        }}
        self.assertEqual(self.policy.decide(reversal, current).reason, "campaign_flip_limit_reached")

    def test_opposite_signal_cannot_flip_an_armed_runner(self):
        current = {
            "timestamp_ms": 1_000,
            "signal": {**self.signal(), "campaign_action": "REVERSAL"},
            "execution": {"full_runner_armed": True, "current_r": -0.50},
        }
        reversal = self.signal("POST_SQUEEZE_REVERSAL", "SHORT")
        reversal["timestamp_ms"] = 6 * 60_000
        reversal["evidence"] = {"confirmation_checks": {
            "structure_reverses": True, "price_reversal": True, "taker_flips": True,
        }}
        decision = self.policy.decide(reversal, current)
        self.assertEqual(decision.action, "HOLD")
        self.assertEqual(decision.reason, "armed_runner_owns_exit")

    def test_partial_and_thesis_exit_are_stateful(self):
        execution = {
            "timestamp_ms": 1_000, "direction": "LONG", "signal": self.signal(),
            "execution": {"fill_price": 100.0, "stop_price": 99.0, "target_price": 102.2},
        }
        self.assertEqual(self.policy.manage(execution, 100.81, 2_000).action, "PARTIAL")
        execution["execution"]["partial_taken"] = True
        execution["execution"]["max_mfe_r"] = 0.2
        execution["execution"]["stop_price"] = 100.1
        execution["execution"]["profit_lock_r"] = 0.10
        review_time = 16 * 60_000 + 1_000
        self.assertEqual(self.policy.manage(execution, 99.8, review_time).action, "HOLD")
        failing_market = {
            "price_change_15m_pct": -0.5, "taker_buy_sell_ratio": 0.8,
            "oi_change_5m_pct": -0.1, "oi_change_15m_pct": -0.2,
        }
        decision = self.policy.manage(execution, 99.8, review_time, failing_market)
        self.assertEqual(decision.action, "EXIT")
        self.assertEqual(decision.reason, "thesis_invalidated")

    def test_failed_reversal_exits_early_only_with_no_progress_and_live_failures(self):
        setup = "VOLATILITY_EXHAUSTION_FADE_SCALP_V1"
        policy = CampaignPolicy({
            **self.SETTINGS,
            "failure_to_launch": {setup: {
                "min_age_minutes": 3, "max_mfe_r": 0.40,
                "max_current_r": -0.20, "min_failures": 2,
            }},
        })
        execution = {
            "timestamp_ms": 1_000, "direction": "LONG",
            "signal": {**self.signal(setup), "campaign_action": "REVERSAL"},
            "execution": {
                "fill_price": 100.0, "initial_stop_price": 99.0,
                "initial_risk_distance": 1.0, "stop_price": 99.0,
                "target_price": 100.8, "max_mfe_r": 0.10,
            },
        }
        market = {
            "price_change_15m_pct": -0.50, "taker_buy_sell_ratio": 0.80,
            "oi_change_5m_pct": 0.10, "oi_change_15m_pct": -0.20,
        }
        decision = policy.manage(execution, 99.70, 4 * 60_000 + 1_000, market)
        self.assertEqual(decision.action, "EXIT")
        self.assertEqual(decision.reason, "reversal_failed_to_launch")

    def test_price_failure_exits_without_waiting_for_delayed_thesis_metrics(self):
        setup = "VOLATILITY_EXHAUSTION_FADE_SCALP_V1"
        policy = CampaignPolicy({
            **self.SETTINGS,
            "price_failure_exit": {setup: {
                "min_age_minutes": 2, "max_mfe_r": 0.30,
                "max_current_r": -0.55,
            }},
        })
        execution = {
            "timestamp_ms": 1_000, "direction": "LONG",
            "signal": {**self.signal(setup), "campaign_action": "REVERSAL"},
            "execution": {
                "fill_price": 100.0, "initial_stop_price": 99.0,
                "initial_risk_distance": 1.0, "stop_price": 99.0,
                "target_price": 100.8, "max_mfe_r": 0.20,
            },
        }
        # The slower derivatives snapshot is still neutral; live mark price alone
        # proves that a no-follow-through entry is already failing materially.
        neutral_market = {
            "price_change_15m_pct": 0.0, "taker_buy_sell_ratio": 1.0,
            "oi_change_5m_pct": 0.0, "oi_change_15m_pct": 0.0,
        }
        decision = policy.manage(execution, 99.40, 3 * 60_000 + 1_000, neutral_market)
        self.assertEqual(decision.action, "EXIT")
        self.assertEqual(decision.reason, "price_failed_without_followthrough")

    def test_continuation_exits_after_partial_progress_is_fully_given_back(self):
        setup = "VOLATILITY_EXHAUSTION_CONTINUATION_SCALP_V1"
        policy = CampaignPolicy({
            **self.SETTINGS,
            "strategy_roles": {setup: "CONFIRM"},
            "failed_progress_exit": {setup: {
                "min_age_minutes": 3, "min_mfe_r": 0.30,
                "max_mfe_r": 0.95, "max_current_r": -0.10,
                "min_failures": 2,
            }},
        })
        execution = {
            "timestamp_ms": 1_000, "direction": "SHORT",
            "signal": {**self.signal(setup, "SHORT"), "campaign_action": "CONFIRM"},
            "execution": {
                "fill_price": 100.0, "initial_stop_price": 101.0,
                "initial_risk_distance": 1.0, "stop_price": 101.0,
                "target_price": 99.0, "max_mfe_r": 0.55,
            },
        }
        market = {
            "price_change_15m_pct": 0.20, "taker_buy_sell_ratio": 1.20,
            "oi_change_5m_pct": -0.10, "oi_change_15m_pct": -0.20,
        }
        decision = policy.manage(execution, 100.20, 4 * 60_000 + 1_000, market)
        self.assertEqual(decision.action, "EXIT")
        self.assertEqual(decision.reason, "progress_failed_after_giveback")

    def test_continuation_giveback_needs_two_live_failures(self):
        setup = "VOLATILITY_EXHAUSTION_CONTINUATION_SCALP_V1"
        policy = CampaignPolicy({
            **self.SETTINGS,
            "strategy_roles": {setup: "CONFIRM"},
            "failed_progress_exit": {setup: {
                "min_age_minutes": 3, "min_mfe_r": 0.30,
                "max_mfe_r": 0.95, "max_current_r": -0.10,
                "min_failures": 2,
            }},
        })
        execution = {
            "timestamp_ms": 1_000, "direction": "SHORT",
            "signal": {**self.signal(setup, "SHORT"), "campaign_action": "CONFIRM"},
            "execution": {
                "fill_price": 100.0, "initial_stop_price": 101.0,
                "initial_risk_distance": 1.0, "stop_price": 101.0,
                "target_price": 99.0, "max_mfe_r": 0.55,
            },
        }
        market = {
            "price_change_15m_pct": -0.20, "taker_buy_sell_ratio": 1.20,
            "oi_change_5m_pct": 0.10, "oi_change_15m_pct": 0.20,
        }
        decision = policy.manage(execution, 100.20, 4 * 60_000 + 1_000, market)
        self.assertEqual(decision.action, "HOLD")

    def test_profitable_campaign_is_not_closed_by_clock(self):
        execution = {
            "timestamp_ms": 1_000, "direction": "LONG", "signal": self.signal(),
            "execution": {
                "fill_price": 100.0, "target_price": 102.2,
                "initial_stop_price": 99.0, "initial_risk_distance": 1.0,
                "stop_price": 100.1, "partial_taken": True, "max_mfe_r": 0.3,
                "profit_lock_r": 0.10,
            },
        }
        failing_market = {
            "price_change_15m_pct": -0.5, "taker_buy_sell_ratio": 0.8,
            "oi_change_5m_pct": -0.1, "oi_change_15m_pct": -0.2,
        }
        decision = self.policy.manage(execution, 100.2, 61 * 60_000, failing_market)
        self.assertEqual(decision.action, "HOLD")

    def test_campaign_has_no_clock_exit_when_time_limit_is_disabled(self):
        policy = CampaignPolicy({**self.SETTINGS, "max_holding_minutes": 0})
        execution = {
            "timestamp_ms": 1_000, "direction": "LONG", "signal": self.signal(),
            "execution": {
                "fill_price": 100.0, "target_price": 102.2,
                "initial_stop_price": 99.0, "initial_risk_distance": 1.0,
                "stop_price": 99.0, "max_mfe_r": 0.0,
            },
        }
        decision = policy.manage(execution, 99.8, 12 * 60 * 60_000)
        self.assertEqual(decision.action, "HOLD")
        self.assertEqual(decision.reason, "campaign_active_without_fresh_thesis")

    def test_metric_only_thesis_does_not_create_automatic_flip(self):
        execution = {
            "id": 7, "timestamp_ms": 1_000, "direction": "LONG",
            "signal": {**self.signal(), "campaign_action": "PROBE", "campaign_id": "c1"},
            "execution": {
                "fill_price": 100.0, "stop_price": 99.0, "initial_stop_price": 99.0,
                "initial_risk_distance": 1.0, "target_price": 102.2,
            },
        }
        market = {
            "symbol": "BTCUSDT", "price": 99.7, "price_change_15m_pct": -0.6,
            "taker_buy_sell_ratio": 0.8, "oi_change_5m_pct": -0.1, "oi_change_15m_pct": -0.2,
            "micro_open": 99.9, "micro_close": 99.7, "micro_previous_low": 99.8,
            "micro_previous_high": 100.0,
        }
        decision = self.policy.manage(execution, 99.7, 4 * 60_000, market)
        self.assertNotEqual(decision.action, "FLIP")

    def test_runner_stop_never_gives_back_below_fee_aware_profit_lock(self):
        execution = {
            "signal": self.signal(),
            "execution": {"fill_price": 100.0, "initial_stop_price": 99.0},
        }
        market = {
            "candle_low": 99.45, "previous_candle_low": 99.30,
            "candle_high": 101.2, "previous_candle_high": 100.8,
        }
        stop = self.policy.runner_stop(execution, 101.0, market)
        self.assertGreaterEqual(stop, 100.1)
        self.assertLess(stop, 101.0)

    def test_profit_ladder_tightens_after_partial_as_mfe_advances(self):
        execution = {
            "timestamp_ms": 1_000, "direction": "LONG", "signal": self.signal(),
            "execution": {
                "fill_price": 100.0, "initial_stop_price": 99.0, "stop_price": 100.1,
                "initial_risk_distance": 1.0, "partial_taken": True, "max_mfe_r": 1.6,
                "profit_lock_r": 0.10,
            },
        }
        decision = self.policy.manage(execution, 101.4, 2_000)
        self.assertEqual(decision.action, "PROTECT")
        self.assertAlmostEqual(decision.signal["stop_price"], 100.3)
        self.assertEqual(decision.signal["lock_r"], 0.30)
        execution["execution"].update(stop_price=100.3, profit_lock_r=0.30, max_mfe_r=2.1)
        decision = self.policy.manage(execution, 101.8, 3_000)
        self.assertEqual(decision.action, "PROTECT")
        self.assertAlmostEqual(decision.signal["stop_price"], 101.0)

    def test_profit_ladder_does_not_submit_a_stop_already_crossed_on_giveback(self):
        execution = {
            "timestamp_ms": 1_000, "direction": "LONG", "signal": self.signal(),
            "execution": {
                "fill_price": 100.0, "initial_stop_price": 99.0, "stop_price": 100.1,
                "initial_risk_distance": 1.0, "partial_taken": True, "max_mfe_r": 2.1,
                "profit_lock_r": 0.10,
            },
        }
        decision = self.policy.manage(execution, 100.2, 3_000)
        self.assertEqual(decision.action, "HOLD")

    def test_dump_full_runner_disables_target_then_trails_entire_position(self):
        policy = CampaignPolicy({
            **self.SETTINGS,
            "full_position_runner_setups": ["DUMP_EXHAUSTION_RECLAIM_V1"],
            "full_runner_activation_lock_r": 0.5,
            "full_runner_min_giveback_pct": 0.65,
            "full_runner_max_giveback_pct": 1.40,
            "full_runner_atr_giveback_multiple": 1.25,
        })
        signal = self.signal("DUMP_EXHAUSTION_RECLAIM_V1")
        signal.update(stop_price=99.0, target_price=102.0, risk_pct=1.0)
        execution = {
            "timestamp_ms": 1_000, "direction": "LONG", "signal": signal,
            "execution": {
                "fill_price": 100.0, "initial_stop_price": 99.0,
                "initial_target_price": 102.0, "stop_price": 99.0,
                "target_price": 102.0, "initial_risk_distance": 1.0,
            },
        }
        self.assertEqual(policy.manage(execution, 100.5, 2_000).action, "ENABLE_RUNNER")
        execution["execution"]["runner_target_disabled"] = True
        market = {
            "atr14_pct": 0.5, "candle_low": 101.4, "previous_candle_low": 101.0,
            "candle_high": 102.2, "previous_candle_high": 101.8,
        }
        armed = policy.manage(execution, 102.1, 3_000, market)
        self.assertEqual(armed.action, "ARM_RUNNER")
        self.assertGreater(armed.signal["stop_price"], 100.0)
        self.assertLess(armed.signal["stop_price"], 102.1)
        execution["execution"].update(
            full_runner_armed=True, stop_price=armed.signal["stop_price"],
            max_mfe_r=2.1, profit_lock_r=armed.signal["lock_r"],
        )
        trailed = policy.manage(execution, 104.0, 4_000, {
            **market, "candle_low": 103.4, "previous_candle_low": 103.0,
            "candle_high": 104.1, "previous_candle_high": 103.8,
        })
        self.assertEqual(trailed.action, "PROTECT")
        self.assertGreater(trailed.signal["stop_price"], armed.signal["stop_price"])

    def test_border_profit_ladder_moves_whole_position_stop_without_partial(self):
        # The scalp runner uses its native +0.8% target only as activation and
        # immediately protects +0.6% before trailing the whole position.
        setup = "VOLATILITY_EXHAUSTION_FADE_SCALP_V1"
        scalp_policy = CampaignPolicy({
            **self.SETTINGS,
            "full_position_runner_setups": [setup],
            "full_runner_activation_lock_pct_overrides": {setup: 0.60},
            "full_runner_min_giveback_pct_overrides": {setup: 0.40},
            "full_runner_max_giveback_pct_overrides": {setup: 0.80},
            "runner_min_price_gap_pct_overrides": {setup: 0.10},
            "full_runner_structure_disabled_setups": [setup],
        })
        scalp_signal = self.signal(setup)
        scalp_signal.update(stop_price=98.0, target_price=100.8, risk_pct=2.0)
        scalp_execution = {
            "timestamp_ms": 1_000, "direction": "LONG", "signal": scalp_signal,
            "execution": {
                "fill_price": 100.0, "initial_stop_price": 98.0,
                "initial_target_price": 100.8, "stop_price": 98.0,
                "target_price": 100.8, "initial_risk_distance": 2.0,
                "runner_target_disabled": True,
            },
        }
        armed = scalp_policy.manage(scalp_execution, 100.81, 2_000)
        self.assertEqual(armed.action, "ARM_RUNNER")
        self.assertAlmostEqual(armed.signal["stop_price"], 100.60)

        # At a new price milestone the explicit Stop Gain ladder must advance
        # even when an ATR giveback would otherwise leave it behind.
        scalp_policy.settings["full_runner_profit_lock_ladders"] = {
            setup: [{"trigger_pct": 1.20, "lock_pct": 0.80}],
        }
        scalp_execution["execution"].update(
            full_runner_armed=True,
            stop_price=armed.signal["stop_price"],
            profit_lock_r=armed.signal["lock_r"],
            max_mfe_r=0.405,
        )
        protected = scalp_policy.manage(
            scalp_execution, 101.25, 3_000, {"atr14_pct": 1.0},
        )
        self.assertEqual(protected.action, "PROTECT")
        self.assertAlmostEqual(protected.signal["stop_price"], 100.80)

        policy = CampaignPolicy({
            **self.SETTINGS,
            "full_position_profit_ladder_setups": ["BORDER_BREAKOUT_RETEST_SHORT"],
            "profit_ladder_1_trigger_r": 1.0, "profit_ladder_1_lock_r": 0.10,
            "profit_ladder_2_trigger_r": 1.5, "profit_ladder_2_lock_r": 0.50,
            "profit_ladder_min_mark_gap_r": 0.08,
        })
        signal = self.signal("BORDER_BREAKOUT_RETEST_SHORT", "SHORT")
        signal.update(stop_price=101.0, target_price=97.8, risk_pct=1.0)
        execution = {
            "timestamp_ms": 1_000, "direction": "SHORT", "signal": signal,
            "execution": {
                "fill_price": 100.0, "initial_stop_price": 101.0,
                "stop_price": 101.0, "initial_risk_distance": 1.0,
                "target_price": 97.8,
            },
        }
        first = policy.manage(execution, 98.9, 2_000)
        self.assertEqual(first.action, "PROTECT")
        self.assertAlmostEqual(first.signal["stop_price"], 99.9)
        execution["execution"].update(
            stop_price=99.9, profit_lock_r=0.10, max_mfe_r=1.1,
        )
        second = policy.manage(execution, 98.3, 3_000)
        self.assertEqual(second.action, "PROTECT")
        self.assertAlmostEqual(second.signal["stop_price"], 99.5)
        self.assertFalse(execution["execution"].get("partial_taken", False))

    def test_dump_continuation_protects_full_position_after_point_eight_percent(self):
        policy = CampaignPolicy({
            **self.SETTINGS,
            "full_position_runner_setups": ["DUMP_CONTINUATION_SHORT"],
            "runner_min_mark_gap_pct": 0.25,
            "strategy_early_profit_protection": {
                "DUMP_CONTINUATION_SHORT": {"trigger_pct": 0.80, "lock_pct": 0.15},
            },
        })
        signal = self.signal("DUMP_CONTINUATION_SHORT", "SHORT")
        signal.update(stop_price=102.1, target_price=96.325, risk_pct=2.1)
        execution = {
            "timestamp_ms": 1_000, "direction": "SHORT", "signal": signal,
            "execution": {
                "fill_price": 100.0, "initial_stop_price": 102.1,
                "stop_price": 102.1, "initial_risk_distance": 2.1,
                "initial_target_price": 96.325, "target_price": 96.325,
                "runner_target_disabled": True,
            },
        }
        decision = policy.manage(execution, 99.1, 2_000)
        self.assertEqual(decision.action, "PROTECT")
        self.assertEqual(decision.reason, "strategy_early_profit_protection")
        self.assertAlmostEqual(decision.signal["stop_price"], 99.85)

    def test_volatility_scalp_can_flip_on_confirmed_exhaustion(self):
        policy = CampaignPolicy({
            **self.SETTINGS,
            "strategy_roles": {"VOLATILITY_EXHAUSTION_FADE_SCALP_V1": "REVERSAL"},
            "strategy_min_stop_pct_overrides": {"VOLATILITY_EXHAUSTION_FADE_SCALP_V1": 2.0},
            "strategy_max_stop_pct_overrides": {"VOLATILITY_EXHAUSTION_FADE_SCALP_V1": 2.0},
            "strategy_target_r_overrides": {"VOLATILITY_EXHAUSTION_FADE_SCALP_V1": 0.4},
            "preserve_native_target_setups": ["VOLATILITY_EXHAUSTION_FADE_SCALP_V1"],
        })
        current = {
            "timestamp_ms": 1_000,
            "signal": {**self.signal("VOLATILITY_EXHAUSTION_FADE_SCALP_V1", "LONG"),
                       "campaign_id": "scalp-1"},
            "execution": {"current_r": -0.50},
        }
        opposite = self.signal("VOLATILITY_EXHAUSTION_FADE_SCALP_V1", "SHORT")
        opposite.update(timestamp_ms=6 * 60_000, stop_price=102.0, target_price=99.2, risk_pct=2.0)
        opposite["evidence"] = {
            "reversal_confirmation_score": 3,
            "confirmation_checks": {
                "late_candle_confirmation": True, "directional_exhaustion": True,
                "body_expansion": True, "rejection_wick": True,
                "rejection_structure_intact": True, "taker_flips": True,
            },
        }
        self.assertEqual(policy.decide(opposite, current).action, "FLIP")

    def test_profitable_scalp_is_not_killed_by_opposite_signal(self):
        policy = CampaignPolicy({
            **self.SETTINGS,
            "strategy_roles": {"VOLATILITY_EXHAUSTION_FADE_SCALP_V1": "REVERSAL"},
            "strategy_min_stop_pct_overrides": {"VOLATILITY_EXHAUSTION_FADE_SCALP_V1": 2.0},
            "strategy_max_stop_pct_overrides": {"VOLATILITY_EXHAUSTION_FADE_SCALP_V1": 2.0},
            "signal_flip_max_current_r": -0.25,
            "signal_flip_min_reversal_score": 3,
        })
        current = {
            "timestamp_ms": 1_000,
            "signal": {**self.signal("VOLATILITY_EXHAUSTION_FADE_SCALP_V1", "SHORT"),
                       "campaign_id": "bless-like"},
            "execution": {"current_r": 0.32, "max_mfe_r": 0.39},
        }
        opposite = self.signal("VOLATILITY_EXHAUSTION_FADE_SCALP_V1", "LONG")
        opposite.update(timestamp_ms=6 * 60_000, stop_price=98.0, target_price=100.8, risk_pct=2.0)
        opposite["evidence"] = {
            "reversal_confirmation_score": 3,
            "confirmation_checks": {
                "late_candle_confirmation": True, "directional_exhaustion": True,
                "body_expansion": True, "rejection_wick": True,
                "rejection_structure_intact": True, "taker_flips": True,
            },
        }
        decision = policy.decide(opposite, current)
        self.assertEqual(decision.action, "HOLD")
        self.assertEqual(decision.reason, "opposite_signal_while_trade_viable")


if __name__ == "__main__":
    unittest.main()
