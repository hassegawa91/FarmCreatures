import unittest

from tools.telegram_event_lab import parse_events


class TelegramEventLabTests(unittest.TestCase):
    def test_parses_multiple_pump_dump_events(self):
        events = parse_events(
            "PUMP: BANKUSDT +11.58% / price 0.521090 "
            "DUMP: AKEUSDT -6.66% / price 0.0045419"
        )
        self.assertEqual([(item.symbol, item.direction) for item in events], [
            ("BANKUSDT", "LONG"), ("AKEUSDT", "SHORT")
        ])
        self.assertEqual(events[0].payload["move_pct"], 11.58)

    def test_liquidated_long_maps_to_sell_pressure(self):
        event = parse_events("#BTC Liquidated Long: $2.11M at $63037.78")[0]
        self.assertEqual(event.event_type, "LIQUIDATION")
        self.assertEqual(event.symbol, "BTCUSDT")
        self.assertEqual(event.direction, "SHORT")
        self.assertEqual(event.payload["notional_usd"], 2_110_000)


    def test_syndicated_signal_has_same_event_key(self):
        a = parse_events(
            "SIGNAL ID: #2197 COIN: $ATOM /USDT (2-5x) Direction: LONG "
            "ENTRY: 1.325 - 1.335 TARGETS: 1.400 - 1.475 STOP LOSS: 1.280"
        )[0]
        b = parse_events(
            "SIGNAL ID:#2197 COIN:$ATOM/USDT Direction:LONG ENTRY:1.325-1.335 "
            "TARGETS 1.400 1.475 STOP LOSS:1.280 All rights reserved"
        )[0]
        self.assertEqual(a.event_key, b.event_key)


    def test_news_is_context_not_directional_trade(self):
        event = parse_events("FED indica possível rate cut; mercado aguarda CPI.")[0]
        self.assertEqual(event.event_type, "INSTITUTIONAL_NEWS")
        self.assertIsNone(event.symbol)
        self.assertIsNone(event.direction)


if __name__ == "__main__":
    unittest.main()
