from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass
from typing import Any

import requests


FAPI = "https://fapi.binance.com"


def number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value if value is not None else default)
    except (TypeError, ValueError):
        return float(default)


def change_pct(current: float, previous: float) -> float:
    return (current - previous) / previous * 100.0 if previous else 0.0


def ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1.0)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1.0 - alpha) * result
    return result


def atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    if len(closes) < 2:
        return 0.0
    ranges = [
        max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i in range(1, len(closes))
    ]
    sample = ranges[-period:]
    return sum(sample) / len(sample) if sample else 0.0


def adx(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 2:
        return 0.0
    trs, plus_dm, minus_dm = [], [], []
    for i in range(1, len(closes)):
        up, down = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    dx_values = []
    for end in range(period, len(trs) + 1):
        tr_sum = sum(trs[end - period:end])
        if tr_sum <= 0:
            continue
        plus_di = 100.0 * sum(plus_dm[end - period:end]) / tr_sum
        minus_di = 100.0 * sum(minus_dm[end - period:end]) / tr_sum
        denominator = plus_di + minus_di
        dx_values.append(100.0 * abs(plus_di - minus_di) / denominator if denominator else 0.0)
    sample = dx_values[-period:]
    return sum(sample) / len(sample) if sample else 0.0


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    timestamp_ms: int
    price: float
    price_change_15m_pct: float
    oi_change_15m_pct: float
    global_lsr: float
    taker_buy_sell_ratio: float
    funding_rate_pct: float
    volume_ratio: float
    spread_pct: float
    prior_high: float
    prior_low: float
    candle_high: float
    candle_low: float
    range_width_pct: float = 0.0
    compression_ratio: float = 999.0
    distance_to_prior_high_pct: float = 999.0
    distance_to_prior_low_pct: float = 999.0
    oi_timestamp_ms: int = 0
    lsr_timestamp_ms: int = 0
    taker_timestamp_ms: int = 0
    oi_change_5m_pct: float = 0.0
    candle_open: float = 0.0
    candle_close: float = 0.0
    candle_open_time_ms: int = 0
    previous_candle_close: float = 0.0
    previous_candle_high: float = 0.0
    previous_candle_low: float = 0.0
    oi_acceleration_5m_pct_points: float = 0.0
    lsr_change_5m_pct: float = 0.0
    directional_candle_count: int = 0
    volume_growth_ratio: float = 0.0
    micro_open: float = 0.0
    micro_high: float = 0.0
    micro_low: float = 0.0
    micro_close: float = 0.0
    micro_previous_high: float = 0.0
    micro_previous_low: float = 0.0
    micro_open_time_ms: int = 0
    micro_reversal_direction: str = ""
    micro_directional_candle_count: int = 0
    micro_reversal_impulse_pct: float = 0.0
    micro_pre_move_pct: float = 0.0
    micro_volume_ratio: float = 0.0
    micro_structure_stop: float = 0.0
    ema21: float = 0.0
    ema21_slope_pct: float = 0.0
    atr14: float = 0.0
    atr14_pct: float = 0.0
    adx14: float = 0.0
    price_change_5m_pct: float = 0.0
    lsr_change_15m_pct: float = 0.0
    trades_ratio: float = 0.0
    edge_range_high: float = 0.0
    edge_range_low: float = 0.0
    edge_range_width_pct: float = 0.0
    edge_position: float = 0.5

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BinancePublicClient:
    def __init__(self, timeout: float = 12.0, base_url: str = FAPI):
        self.timeout = timeout
        self.base_url = str(base_url or FAPI).rstrip("/")
        self._local = threading.local()
        self.quote_volumes: dict[str, float] = {}

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update({"User-Agent": "v10-oi-shadow/1.0"})
            self._local.session = session
        return session

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = self._session().get(self.base_url + path, params=params or {}, timeout=self.timeout)
        if response.status_code in (418, 429):
            retry = response.headers.get("Retry-After") or "60"
            raise RuntimeError(f"Binance rate limit {response.status_code}; retry_after={retry}")
        response.raise_for_status()
        return response.json()

    def universe(self, size: int, min_quote_volume: float, always_include: list[str]) -> list[str]:
        info = self.get("/fapi/v1/exchangeInfo")
        active = {
            row["symbol"]
            for row in info.get("symbols", [])
            if row.get("status") == "TRADING"
            and row.get("contractType") == "PERPETUAL"
            and row.get("quoteAsset") == "USDT"
        }
        tickers = self.get("/fapi/v1/ticker/24hr")
        self.quote_volumes = {
            str(row.get("symbol")): number(row.get("quoteVolume"))
            for row in tickers if row.get("symbol") in active
        }
        ranked = sorted(
            (
                (number(row.get("quoteVolume")), row.get("symbol"))
                for row in tickers
                if row.get("symbol") in active and number(row.get("quoteVolume")) >= min_quote_volume
            ),
            reverse=True,
        )
        result = [symbol for symbol in always_include if symbol in active]
        for _, symbol in ranked:
            if symbol not in result:
                result.append(symbol)
            if size > 0 and len(result) >= size:
                break
        return result

    def price_context(self, symbol: str, strategy: dict[str, Any]) -> dict[str, Any]:
        now_ms = int(time.time() * 1000)
        klines = self._closed_klines(
            self.get("/fapi/v1/klines", {"symbol": symbol, "interval": "5m", "limit": 40}),
            now_ms,
        )
        if len(klines) < 20:
            raise RuntimeError(f"historico insuficiente para {symbol}")
        closes = [number(row[4]) for row in klines]
        opens = [number(row[1]) for row in klines]
        highs = [number(row[2]) for row in klines]
        lows = [number(row[3]) for row in klines]
        volumes = [number(row[7]) for row in klines]
        # Binance supplies the trade count at index 8. Historical fixtures and
        # degraded feeds may stop at quote volume, where volume is the safest
        # neutral activity proxy instead of crashing the whole scan.
        trades = [number(row[8]) if len(row) > 8 else number(row[7]) for row in klines]
        impulse_bars = int(strategy.get("impulse_lookback_bars", 3))
        breakout_bars = int(strategy.get("breakout_lookback_bars", 12))
        recent_volume = sum(volumes[-impulse_bars:]) / impulse_bars
        baseline_rows = volumes[-20:-impulse_bars]
        baseline_volume = sum(baseline_rows) / max(1, len(baseline_rows))
        prior_slice = slice(-breakout_bars - impulse_bars, -impulse_bars)
        price = closes[-1]
        ema21_value = ema(closes, 21)
        ema21_previous = ema(closes[:-1], 21)
        atr14_value = atr(highs, lows, closes, 14)
        adx14_value = adx(highs, lows, closes, 14)
        impulse = change_pct(price, closes[-1 - impulse_bars])
        prior_high, prior_low = max(highs[prior_slice]), min(lows[prior_slice])
        volume_ratio = recent_volume / baseline_volume if baseline_volume else 0.0
        prior_two_volume = sum(volumes[-3:-1]) / 2.0
        volume_growth_ratio = volumes[-1] / prior_two_volume if prior_two_volume else 0.0
        trades_baseline = sum(trades[-20:-1]) / max(1, len(trades[-20:-1]))
        trades_ratio = trades[-1] / trades_baseline if trades_baseline else 0.0
        price_change_5m_pct = change_pct(price, closes[-2])
        candle_direction = 1 if impulse > 0 else -1
        directional_candle_count = 0
        for index in range(len(closes) - 1, max(-1, len(closes) - 5), -1):
            body_direction = 1 if closes[index] > opens[index] else -1 if closes[index] < opens[index] else 0
            if body_direction != candle_direction:
                break
            directional_candle_count += 1
        compression_bars = int(strategy.get("prefilter_compression_bars", 6))
        recent_ranges = [
            (highs[index] - lows[index]) / closes[index] * 100.0 if closes[index] else 0.0
            for index in range(max(0, len(closes) - compression_bars), len(closes))
        ]
        baseline_start = max(0, len(closes) - compression_bars - 20)
        baseline_end = max(baseline_start, len(closes) - compression_bars)
        baseline_ranges = [
            (highs[index] - lows[index]) / closes[index] * 100.0 if closes[index] else 0.0
            for index in range(baseline_start, baseline_end)
        ]
        recent_range_avg = sum(recent_ranges) / max(1, len(recent_ranges))
        baseline_range_avg = sum(baseline_ranges) / max(1, len(baseline_ranges))
        compression_ratio = recent_range_avg / baseline_range_avg if baseline_range_avg else 999.0
        recent_high = max(highs[-compression_bars:])
        recent_low = min(lows[-compression_bars:])
        range_width_pct = (recent_high - recent_low) / price * 100.0 if price else 999.0
        distance_high = (prior_high - price) / price * 100.0 if price else 999.0
        distance_low = (price - prior_low) / price * 100.0 if price else 999.0
        edge_lookback = max(6, int(strategy.get("border_range_lookback_bars", 12)))
        edge_highs = highs[-edge_lookback - 1:-1]
        edge_lows = lows[-edge_lookback - 1:-1]
        edge_range_high = max(edge_highs) if edge_highs else prior_high
        edge_range_low = min(edge_lows) if edge_lows else prior_low
        edge_span = edge_range_high - edge_range_low
        edge_range_width_pct = edge_span / price * 100.0 if price and edge_span > 0 else 0.0
        edge_position = (price - edge_range_low) / edge_span if edge_span > 0 else 0.5
        threshold = float(strategy["min_price_impulse_pct"])
        expansion_passes = volume_ratio >= float(strategy["min_volume_ratio"]) and (
            (impulse >= threshold and price > prior_high)
            or (impulse <= -threshold and price < prior_low)
        )
        near_boundary = (
            -float(strategy.get("prefilter_max_extension_pct", 0.05)) <= distance_high <= float(strategy.get("prefilter_near_boundary_pct", 0.40))
            or -float(strategy.get("prefilter_max_extension_pct", 0.05)) <= distance_low <= float(strategy.get("prefilter_near_boundary_pct", 0.40))
        )
        pre_explosion_passes = (
            compression_ratio <= float(strategy.get("prefilter_max_compression_ratio", 0.95))
            and range_width_pct <= float(strategy.get("prefilter_max_range_width_pct", 1.80))
            and volume_ratio >= float(strategy.get("prefilter_min_volume_ratio", 0.65))
            and near_boundary
        )
        signed_impulse = abs(impulse)
        momentum_breakout = price > prior_high if impulse > 0 else price < prior_low
        momentum_passes = (
            directional_candle_count >= int(strategy.get("momentum_min_directional_candles", 2))
            and float(strategy.get("momentum_min_impulse_pct", 1.20)) <= signed_impulse <= float(strategy.get("momentum_max_arm_impulse_pct", 3.0))
            and momentum_breakout
            and volume_ratio >= float(strategy.get("momentum_min_volume_ratio", 0.75))
            and volume_growth_ratio >= float(strategy.get("momentum_min_volume_growth_ratio", 1.15))
        )
        dump_research_passes = price_change_5m_pct <= -float(
            strategy.get("dump_reclaim_arm_price_5m_pct", 2.0)
        )
        edge_distance_pct = min(
            abs(price - edge_range_low), abs(price - edge_range_high)
        ) / price * 100.0 if price else 999.0
        border_research_passes = bool(strategy.get("border_enabled", False)) and (
            float(strategy.get("border_prefilter_min_range_width_pct", 0.65))
            <= edge_range_width_pct
            <= float(strategy.get("border_prefilter_max_range_width_pct", 4.0))
            and (
                adx14_value <= float(strategy.get("border_prefilter_max_adx", 30.0))
                or edge_distance_pct <= float(strategy.get("border_prefilter_max_edge_distance_pct", 0.60))
            )
        )
        causal_dump_only = str(strategy.get("name") or "") in {
            "DUMP_EXHAUSTION_RECLAIM_V1", "DUMP_REGIME_V2",
        }
        return {
            "timestamp_ms": now_ms, "price": price, "price_change_15m_pct": impulse,
            "volume_ratio": volume_ratio, "prior_high": prior_high, "prior_low": prior_low,
            "candle_high": highs[-1], "candle_low": lows[-1],
            "range_width_pct": range_width_pct, "compression_ratio": compression_ratio,
            "distance_to_prior_high_pct": distance_high, "distance_to_prior_low_pct": distance_low,
            "candle_open": number(klines[-1][1]), "candle_close": number(klines[-1][4]),
            "candle_open_time_ms": int(klines[-1][0]),
            "previous_candle_close": number(klines[-2][4]),
            "previous_candle_high": number(klines[-2][2]),
            "previous_candle_low": number(klines[-2][3]),
            "directional_candle_count": directional_candle_count,
            "volume_growth_ratio": volume_growth_ratio,
            "trades_ratio": trades_ratio,
            "price_change_5m_pct": price_change_5m_pct,
            "edge_range_high": edge_range_high,
            "edge_range_low": edge_range_low,
            "edge_range_width_pct": edge_range_width_pct,
            "edge_position": edge_position,
            "ema21": ema21_value,
            "ema21_slope_pct": change_pct(ema21_value, ema21_previous),
            "atr14": atr14_value,
            "atr14_pct": atr14_value / price * 100.0 if price else 0.0,
            "adx14": adx14_value,
            "passes": (dump_research_passes or border_research_passes) if causal_dump_only else (
                expansion_passes or pre_explosion_passes or momentum_passes
            ),
            "expansion_passes": expansion_passes, "pre_explosion_passes": pre_explosion_passes,
            "momentum_passes": momentum_passes,
        }

    def micro_reversal_context(self, symbol: str, strategy: dict[str, Any]) -> dict[str, Any]:
        """Cheap 1m prefilter for the first structured turn after a short exhaustion move."""
        now_ms = int(time.time() * 1000)
        rows = self._closed_klines(
            self.get("/fapi/v1/klines", {"symbol": symbol, "interval": "1m", "limit": 12}),
            now_ms,
        )
        if len(rows) < 10:
            raise RuntimeError(f"historico 1m insuficiente para {symbol}")
        opens = [number(row[1]) for row in rows]
        highs = [number(row[2]) for row in rows]
        lows = [number(row[3]) for row in rows]
        closes = [number(row[4]) for row in rows]
        quote_volumes = [number(row[7]) for row in rows]
        required = int(strategy.get("early_reversal_min_directional_candles", 3))
        required = max(2, min(required, 4))
        recent = range(len(rows) - required, len(rows))
        long_streak = all(closes[i] > opens[i] for i in recent) and all(
            closes[i] > closes[i - 1] for i in range(len(rows) - required + 1, len(rows))
        )
        short_streak = all(closes[i] < opens[i] for i in recent) and all(
            closes[i] < closes[i - 1] for i in range(len(rows) - required + 1, len(rows))
        )
        turn_index = len(rows) - required - 1
        anchor_index = max(0, turn_index - int(strategy.get("early_reversal_pre_move_bars", 4)))
        pre_move = change_pct(closes[turn_index], closes[anchor_index])
        reversal_impulse = change_pct(closes[-1], closes[turn_index])
        baseline = quote_volumes[max(0, len(rows) - required - 5):len(rows) - required]
        recent_volume = quote_volumes[-required:]
        baseline_avg = sum(baseline) / max(1, len(baseline))
        recent_avg = sum(recent_volume) / max(1, len(recent_volume))
        volume_ratio = recent_avg / baseline_avg if baseline_avg else 0.0
        min_pre_move = float(strategy.get("early_reversal_min_pre_move_pct", 0.35))
        min_reversal = float(strategy.get("early_reversal_min_impulse_pct", 0.15))
        max_reversal = float(strategy.get("early_reversal_max_impulse_pct", 0.90))
        min_volume = float(strategy.get("early_reversal_min_micro_volume_ratio", 1.10))
        long_passes = (
            long_streak and pre_move <= -min_pre_move
            and min_reversal <= reversal_impulse <= max_reversal
            and volume_ratio >= min_volume
        )
        short_passes = (
            short_streak and pre_move >= min_pre_move
            and -max_reversal <= reversal_impulse <= -min_reversal
            and volume_ratio >= min_volume
        )
        direction = "LONG" if long_passes else "SHORT" if short_passes else ""
        structure_slice = slice(turn_index, len(rows))
        structure_stop = (
            min(lows[structure_slice]) if direction == "LONG"
            else max(highs[structure_slice]) if direction == "SHORT" else 0.0
        )
        return {
            "micro_reversal_passes": bool(direction),
            "micro_reversal_direction": direction,
            "micro_directional_candle_count": required if direction else 0,
            "micro_reversal_impulse_pct": reversal_impulse,
            "micro_pre_move_pct": pre_move,
            "micro_volume_ratio": volume_ratio,
            "micro_structure_stop": structure_stop,
        }

    @staticmethod
    def _closed_klines(rows: list[list[Any]], now_ms: int) -> list[list[Any]]:
        return [row for row in rows if int(row[6]) < now_ms]

    def snapshot(self, symbol: str, strategy: dict[str, Any], context: dict[str, Any] | None = None) -> MarketSnapshot:
        context = context or self.price_context(symbol, strategy)
        now_ms = int(context["timestamp_ms"])
        oi = self.get("/futures/data/openInterestHist", {"symbol": symbol, "period": "5m", "limit": 5})
        lsr = self.get("/futures/data/globalLongShortAccountRatio", {"symbol": symbol, "period": "5m", "limit": 4})
        taker = self.get("/futures/data/takerlongshortRatio", {"symbol": symbol, "period": "5m", "limit": 2})
        premium = self.get("/fapi/v1/premiumIndex", {"symbol": symbol})
        book = self.get("/fapi/v1/ticker/bookTicker", {"symbol": symbol})
        micro = self._closed_klines(
            self.get("/fapi/v1/klines", {"symbol": symbol, "interval": "1m", "limit": 4}),
            now_ms,
        )
        if len(oi) < 5 or len(lsr) < 4 or not taker or len(micro) < 2:
            raise RuntimeError(f"historico insuficiente para {symbol}")
        oi_values = [number(row.get("sumOpenInterest")) for row in oi]
        lsr_values = [number(row.get("longShortRatio")) for row in lsr]
        current_oi_15m = change_pct(oi_values[-1], oi_values[-4])
        previous_oi_15m = change_pct(oi_values[-2], oi_values[-5])
        bid, ask = number(book.get("bidPrice")), number(book.get("askPrice"))
        mid = (bid + ask) / 2.0
        price = number(premium.get("markPrice"), context["price"])
        return MarketSnapshot(
            symbol=symbol,
            timestamp_ms=now_ms,
            price=price,
            price_change_15m_pct=number(context["price_change_15m_pct"]),
            oi_change_15m_pct=current_oi_15m,
            global_lsr=lsr_values[-1],
            taker_buy_sell_ratio=number(taker[-1].get("buySellRatio")),
            funding_rate_pct=number(premium.get("lastFundingRate")) * 100.0,
            volume_ratio=number(context["volume_ratio"]),
            spread_pct=(ask - bid) / mid * 100.0 if mid else 999.0,
            prior_high=number(context["prior_high"]),
            prior_low=number(context["prior_low"]),
            candle_high=number(context["candle_high"]),
            candle_low=number(context["candle_low"]),
            range_width_pct=number(context.get("range_width_pct"), 999.0),
            compression_ratio=number(context.get("compression_ratio"), 999.0),
            distance_to_prior_high_pct=number(context.get("distance_to_prior_high_pct"), 999.0),
            distance_to_prior_low_pct=number(context.get("distance_to_prior_low_pct"), 999.0),
            oi_timestamp_ms=int(oi[-1].get("timestamp") or 0),
            lsr_timestamp_ms=int(lsr[-1].get("timestamp") or 0),
            taker_timestamp_ms=int(taker[-1].get("timestamp") or 0),
            oi_change_5m_pct=change_pct(oi_values[-1], oi_values[-2]),
            candle_open=number(context.get("candle_open")),
            candle_close=number(context.get("candle_close")),
            candle_open_time_ms=int(context.get("candle_open_time_ms") or 0),
            previous_candle_close=number(context.get("previous_candle_close")),
            previous_candle_high=number(context.get("previous_candle_high")),
            previous_candle_low=number(context.get("previous_candle_low")),
            oi_acceleration_5m_pct_points=current_oi_15m - previous_oi_15m,
            lsr_change_5m_pct=change_pct(lsr_values[-1], lsr_values[-2]),
            directional_candle_count=int(context.get("directional_candle_count") or 0),
            volume_growth_ratio=number(context.get("volume_growth_ratio")),
            micro_open=number(micro[-1][1]),
            micro_high=number(micro[-1][2]),
            micro_low=number(micro[-1][3]),
            micro_close=number(micro[-1][4]),
            micro_previous_high=number(micro[-2][2]),
            micro_previous_low=number(micro[-2][3]),
            micro_open_time_ms=int(micro[-1][0]),
            micro_reversal_direction=str(context.get("micro_reversal_direction") or ""),
            micro_directional_candle_count=int(context.get("micro_directional_candle_count") or 0),
            micro_reversal_impulse_pct=number(context.get("micro_reversal_impulse_pct")),
            micro_pre_move_pct=number(context.get("micro_pre_move_pct")),
            micro_volume_ratio=number(context.get("micro_volume_ratio")),
            micro_structure_stop=number(context.get("micro_structure_stop")),
            ema21=number(context.get("ema21")),
            ema21_slope_pct=number(context.get("ema21_slope_pct")),
            atr14=number(context.get("atr14")),
            atr14_pct=number(context.get("atr14_pct")),
            adx14=number(context.get("adx14")),
            price_change_5m_pct=number(context.get("price_change_5m_pct")),
            lsr_change_15m_pct=change_pct(lsr_values[-1], lsr_values[-4]),
            trades_ratio=number(context.get("trades_ratio")),
            edge_range_high=number(context.get("edge_range_high")),
            edge_range_low=number(context.get("edge_range_low")),
            edge_range_width_pct=number(context.get("edge_range_width_pct")),
            edge_position=number(context.get("edge_position"), 0.5),
        )
