import unittest

from engine.binance import BinancePublicClient


class PublicClientTests(unittest.TestCase):
    def test_micro_reversal_prefilter_detects_third_green_candle_after_drop(self):
        client = BinancePublicClient()
        closes = [100.0, 99.8, 99.6, 99.4, 99.2, 99.0, 98.9, 98.8, 98.7, 98.9, 99.1, 99.3]
        rows = []
        for index, close in enumerate(closes):
            open_price = close + 0.08 if index < 9 else close - 0.10
            volume = 100.0 if index < 9 else 160.0
            rows.append([
                index * 60_000, str(open_price), str(max(open_price, close) + 0.04),
                str(min(open_price, close) - 0.04), str(close), "0",
                index * 60_000 + 59_999, str(volume),
            ])
        client.get = lambda *args, **kwargs: rows
        context = client.micro_reversal_context("TESTUSDT", {
            "early_reversal_min_directional_candles": 3,
            "early_reversal_pre_move_bars": 4,
            "early_reversal_min_pre_move_pct": 0.35,
            "early_reversal_min_impulse_pct": 0.15,
            "early_reversal_max_impulse_pct": 1.0,
            "early_reversal_min_micro_volume_ratio": 1.1,
        })
        self.assertTrue(context["micro_reversal_passes"])
        self.assertEqual(context["micro_reversal_direction"], "LONG")
        self.assertEqual(context["micro_directional_candle_count"], 3)

    def test_momentum_prefilter_keeps_early_staircase_for_heavy_metrics(self):
        client = BinancePublicClient()
        rows = []
        for index in range(40):
            open_price = 100.0
            close_price = 100.0
            volume = 100.0
            if index == 37:
                close_price, volume = 100.6, 100.0
            elif index == 38:
                open_price, close_price, volume = 100.6, 101.4, 150.0
            elif index == 39:
                open_price, close_price, volume = 101.4, 102.4, 270.0
            rows.append([
                index * 300_000, str(open_price), str(max(open_price, close_price) + 0.1),
                str(min(open_price, close_price) - 0.1), str(close_price), "0",
                index * 300_000 + 299_999, str(volume),
            ])
        client.get = lambda *args, **kwargs: rows
        context = client.price_context("TESTUSDT", {
            "impulse_lookback_bars": 3, "breakout_lookback_bars": 12,
            "min_price_impulse_pct": 0.7, "min_volume_ratio": 3.0,
            "pre_explosion_compression_bars": 6,
            "pre_explosion_max_compression_ratio": 0.2,
            "pre_explosion_max_range_width_pct": 0.2,
            "pre_explosion_min_pre_volume_ratio": 3.0,
            "pre_explosion_max_prearm_extension_pct": 0.08,
            "pre_explosion_near_boundary_pct": 0.25,
            "momentum_min_directional_candles": 2,
            "momentum_min_impulse_pct": 1.2,
            "momentum_max_arm_impulse_pct": 3.0,
            "momentum_min_volume_ratio": 0.75,
            "momentum_min_volume_growth_ratio": 1.15,
        })
        self.assertTrue(context["momentum_passes"])
        self.assertTrue(context["passes"])

    def test_zero_universe_size_means_no_quantity_cap(self):
        client = BinancePublicClient()
        responses = [
            {"symbols": [
                {"symbol": "AAAUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"},
                {"symbol": "BBBUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"},
            ]},
            [
                {"symbol": "AAAUSDT", "quoteVolume": "20000000"},
                {"symbol": "BBBUSDT", "quoteVolume": "15000000"},
            ],
        ]
        client.get = lambda *args, **kwargs: responses.pop(0)
        self.assertEqual(client.universe(0, 10_000_000, []), ["AAAUSDT", "BBBUSDT"])


if __name__ == "__main__":
    unittest.main()
