from __future__ import annotations

import hashlib
import hmac
import os
import time
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests


def number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value if value is not None else default)
    except (TypeError, ValueError):
        return float(default)


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class RiskSizer:
    @staticmethod
    def quantity(
        wallet_balance: float,
        entry_price: float,
        stop_price: float,
        leverage: int,
        account_risk_pct: float,
        max_margin_pct: float,
        round_trip_fee_rate: float = 0.0,
        fixed_margin_usdt: float = 0.0,
    ) -> float:
        stop_distance = abs(entry_price - stop_price)
        if min(wallet_balance, entry_price, stop_distance) <= 0:
            raise ValueError("wallet, entry e stop devem ser positivos")
        margin_cap = wallet_balance * max_margin_pct / 100.0
        if fixed_margin_usdt > 0:
            margin_budget = min(float(fixed_margin_usdt), margin_cap)
            return margin_budget * leverage / entry_price
        risk_budget = wallet_balance * account_risk_pct / 100.0
        loss_distance = stop_distance + entry_price * max(0.0, round_trip_fee_rate)
        by_risk = risk_budget / loss_distance
        by_margin = margin_cap * leverage / entry_price
        return min(by_risk, by_margin)

    @staticmethod
    def floor_step(value: float, step: float) -> Decimal:
        raw, quantum = Decimal(str(value)), Decimal(str(step))
        return (raw / quantum).to_integral_value(rounding=ROUND_DOWN) * quantum

    @staticmethod
    def nearest_step(value: float, step: float) -> Decimal:
        raw, quantum = Decimal(str(value)), Decimal(str(step))
        return (raw / quantum).to_integral_value(rounding=ROUND_HALF_UP) * quantum

    @staticmethod
    def trigger_price(value: float, tick: float, round_up: bool) -> Decimal:
        raw, quantum = Decimal(str(value)), Decimal(str(tick))
        rounding = ROUND_UP if round_up else ROUND_DOWN
        return (raw / quantum).to_integral_value(rounding=rounding) * quantum


class BinanceFuturesBroker:
    def __init__(self, mode: str, settings: dict[str, Any], root: Path):
        self.mode = mode.upper()
        if self.mode not in {"TESTNET", "REAL"}:
            raise ValueError("broker Binance exige TESTNET ou REAL")
        if not settings.get("enabled"):
            raise RuntimeError("execution.enabled=false")
        load_env(root / ".env")
        if self.mode == "REAL":
            expected = str(settings.get("real_confirmation_value") or "")
            actual = os.environ.get(str(settings.get("real_confirmation_env") or ""), "")
            if not expected or actual != expected:
                raise RuntimeError("confirmacao externa de trading REAL ausente")
        prefix = "BINANCE_TESTNET" if self.mode == "TESTNET" else "BINANCE_REAL"
        self.api_key = os.environ.get(prefix + "_API_KEY", "").strip()
        self.api_secret = os.environ.get(prefix + "_API_SECRET", "").strip()
        if not self.api_key or not self.api_secret:
            raise RuntimeError(f"credenciais {prefix} ausentes")
        self.settings = settings
        self.base_url = settings["testnet_base_url"] if self.mode == "TESTNET" else settings["real_base_url"]
        self.session = requests.Session()
        self.session.headers.update({"X-MBX-APIKEY": self.api_key, "User-Agent": "v10-execution/1.0"})
        self.time_offset_ms = 0
        self.exchange_info_cache: dict[str, Any] | None = None
        self.commission_cache: dict[str, dict[str, float]] = {}

    def commission_rates(self, symbol: str) -> dict[str, float]:
        cache = getattr(self, "commission_cache", None)
        if cache is None:
            cache = {}
            self.commission_cache = cache
        cached = cache.get(symbol)
        if cached is not None:
            return cached
        fallback = float(self.settings.get("fallback_taker_fee_pct", 0.05)) / 100.0
        try:
            row = self.request("GET", "/fapi/v1/commissionRate", {"symbol": symbol})
            rates = {
                "maker": number(row.get("makerCommissionRate"), fallback),
                "taker": number(row.get("takerCommissionRate"), fallback),
            }
        except Exception:
            rates = {"maker": fallback, "taker": fallback}
        cache[symbol] = rates
        return rates

    def _server_time(self) -> int:
        response = self.session.get(self.base_url + "/fapi/v1/time", timeout=10)
        response.raise_for_status()
        return int(response.json()["serverTime"])

    def sync_time(self) -> None:
        local = int(time.time() * 1000)
        self.time_offset_ms = self._server_time() - local

    def request(self, method: str, path: str, params: dict[str, Any] | None = None, signed: bool = True) -> Any:
        payload = {key: value for key, value in (params or {}).items() if value is not None}
        if signed:
            # Binance rejects timestamps more than 1000 ms ahead even with a large recvWindow.
            # Keep signed requests slightly behind the synchronized server clock.
            safety_ms = max(0, int(self.settings.get("timestamp_safety_ms", 500)))
            payload["timestamp"] = int(time.time() * 1000) + self.time_offset_ms - safety_ms
            payload["recvWindow"] = int(self.settings.get("recv_window", 5000))
            query = urlencode(payload)
            payload["signature"] = hmac.new(self.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        response = self.session.request(method, self.base_url + path, params=payload, timeout=15)
        try:
            data = response.json()
        except Exception:
            data = {"message": response.text[:500]}
        if response.status_code >= 400:
            if isinstance(data, dict) and data.get("code") == -1021:
                self.sync_time()
            raise RuntimeError(f"Binance {response.status_code}: {data}")
        return data

    def exchange_info(self) -> dict[str, Any]:
        if self.exchange_info_cache is None:
            self.exchange_info_cache = self.request("GET", "/fapi/v1/exchangeInfo", signed=False)
        return self.exchange_info_cache

    def symbol_rules(self, symbol: str) -> dict[str, float]:
        row = next((item for item in self.exchange_info().get("symbols", []) if item.get("symbol") == symbol), None)
        if not row or row.get("status") != "TRADING":
            raise RuntimeError(f"simbolo indisponivel no ambiente {self.mode}: {symbol}")
        filters = {item["filterType"]: item for item in row.get("filters", [])}
        lot = filters.get("MARKET_LOT_SIZE") or filters.get("LOT_SIZE") or {}
        price = filters.get("PRICE_FILTER") or {}
        notional = filters.get("MIN_NOTIONAL") or {}
        return {
            "step_size": number(lot.get("stepSize")), "min_qty": number(lot.get("minQty")),
            "max_qty": number(lot.get("maxQty"), 1e18), "tick_size": number(price.get("tickSize")),
            "min_notional": number(notional.get("notional")),
        }

    def supported_symbols(self) -> set[str]:
        return {
            str(row["symbol"])
            for row in self.exchange_info().get("symbols", [])
            if row.get("status") == "TRADING"
            and row.get("contractType") == "PERPETUAL"
            and row.get("quoteAsset") == "USDT"
        }

    def standard_margin_symbols(self) -> set[str]:
        """Symbols whose venue limits can hold the configured fixed-margin position."""
        supported = self.supported_symbols()
        fixed_margin = float(self.settings.get("fixed_margin_usdt", 0.0))
        if fixed_margin <= 0:
            return supported
        leverage = int(self.settings["leverage"])
        tolerance = float(self.settings.get("fixed_margin_tolerance_pct", 2.0))
        rows = self.request("GET", "/fapi/v1/premiumIndex", signed=False)
        marks = {
            str(row.get("symbol") or ""): number(row.get("markPrice"))
            for row in (rows if isinstance(rows, list) else [rows])
        }
        eligible: set[str] = set()
        for symbol in supported:
            mark = marks.get(symbol, 0.0)
            if mark <= 0:
                continue
            rules = self.symbol_rules(symbol)
            if rules["step_size"] <= 0:
                continue
            requested = fixed_margin * leverage / mark
            capped = min(requested, rules["max_qty"])
            qty = RiskSizer.nearest_step(capped, rules["step_size"])
            if float(qty) > rules["max_qty"]:
                qty = RiskSizer.floor_step(rules["max_qty"], rules["step_size"])
            notional = float(qty) * mark
            effective_margin = notional / leverage
            deviation = abs(effective_margin - fixed_margin) / fixed_margin * 100.0
            if (
                qty >= Decimal(str(rules["min_qty"]))
                and notional >= rules["min_notional"]
                and deviation <= tolerance
            ):
                eligible.add(symbol)
        return eligible

    def account(self) -> dict[str, Any]:
        return self.request("GET", "/fapi/v3/account")

    def positions(self) -> list[dict[str, Any]]:
        rows = self.request("GET", "/fapi/v3/positionRisk")
        return rows if isinstance(rows, list) else [rows]

    def open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        rows = self.request("GET", "/fapi/v1/openOrders", {"symbol": symbol} if symbol else None)
        return rows if isinstance(rows, list) else [rows]

    def open_algo_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        rows = self.request("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol} if symbol else None)
        return rows if isinstance(rows, list) else [rows]

    def user_trades(self, symbol: str, start_time: int) -> list[dict[str, Any]]:
        rows = self.request("GET", "/fapi/v1/userTrades", {
            "symbol": symbol, "startTime": start_time, "limit": 1000,
        })
        return rows if isinstance(rows, list) else [rows]

    def account_state(self, monitored_symbols: set[str] | None = None) -> dict[str, Any]:
        live = [row for row in self.positions() if abs(number(row.get("positionAmt"))) > 0]
        if monitored_symbols is None:
            orders, algo_orders = self.open_orders(), self.open_algo_orders()
        else:
            symbols = sorted(set(monitored_symbols) | {str(row.get("symbol") or "") for row in live})
            orders, algo_orders = [], []
            for symbol in (item for item in symbols if item):
                orders.extend(self.open_orders(symbol))
                algo_orders.extend(self.open_algo_orders(symbol))
        return {"mode": self.mode, "positions": live, "orders": orders, "algo_orders": algo_orders}

    def close_all(self) -> dict[str, Any]:
        state = self.account_state()
        actions, errors = [], []
        touched = set()
        for row in state["positions"]:
            symbol = str(row.get("symbol") or "")
            amount = number(row.get("positionAmt"))
            if not symbol or not amount:
                continue
            touched.add(symbol)
            try:
                rules = self.symbol_rules(symbol)
                qty = RiskSizer.floor_step(abs(amount), rules["step_size"])
                result = self._market_close(symbol, "SELL" if amount > 0 else "BUY", format(qty, "f"))
                actions.append({"symbol": symbol, "order_id": result.get("orderId"), "status": result.get("status")})
            except Exception as exc:
                errors.append(f"{symbol}: {exc}")
        touched.update(str(row.get("symbol") or "") for row in state["orders"] + state["algo_orders"])
        for symbol in sorted(item for item in touched if item):
            try:
                self.request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})
            except Exception as exc:
                errors.append(f"{symbol} normal orders: {exc}")
        for order in state["algo_orders"]:
            symbol, algo_id = str(order.get("symbol") or ""), order.get("algoId")
            if not algo_id:
                continue
            try:
                self.request("DELETE", "/fapi/v1/algoOrder", {"symbol": symbol, "algoId": algo_id})
            except Exception as exc:
                errors.append(f"{symbol} algo order {algo_id}: {exc}")
        time.sleep(0.5)
        remaining = self.account_state()
        ok = not remaining["positions"] and not remaining["orders"] and not remaining["algo_orders"]
        return {"ok": ok, "mode": self.mode, "actions": actions, "errors": errors, "remaining": remaining}

    def reconcile(self, pending: list[dict[str, Any]], state: dict[str, Any]) -> list[dict[str, Any]]:
        live_symbols = {str(row.get("symbol")) for row in state["positions"]}
        completed = []
        for item in pending:
            symbol = str(item["symbol"])
            if symbol in live_symbols:
                continue
            execution = item["execution"]
            signal = item["signal"]
            entry_order_id = str(execution.get("open_order_id") or "")
            trades = sorted(self.user_trades(symbol, int(item["timestamp_ms"])), key=lambda row: int(row.get("time") or 0))
            entry_index = next(
                (index for index, row in enumerate(trades) if str(row.get("orderId") or "") == entry_order_id),
                None,
            )
            if entry_index is None:
                continue
            relevant, signed_quantity = [], 0.0
            entry_side = "BUY" if item["direction"] == "LONG" else "SELL"
            saw_exit = False
            for row in trades[entry_index:]:
                quantity = number(row.get("qty"))
                side = str(row.get("side") or "")
                signed_quantity += quantity if side == "BUY" else -quantity
                relevant.append(row)
                if side and side != entry_side:
                    saw_exit = True
                tolerance = max(1e-12, sum(number(trade.get("qty")) for trade in relevant) * 1e-9)
                if saw_exit and abs(signed_quantity) <= tolerance:
                    break
            if not saw_exit or abs(signed_quantity) > tolerance:
                continue
            entries = [row for row in relevant if str(row.get("side")) == entry_side]
            exits = [row for row in relevant if str(row.get("side")) != entry_side]
            entry_qty = sum(number(row.get("qty")) for row in entries)
            exit_qty = sum(number(row.get("qty")) for row in exits)
            if not entry_qty or not exit_qty:
                continue
            entry_quote = sum(number(row.get("price")) * number(row.get("qty")) for row in entries)
            exit_quote = sum(number(row.get("price")) * number(row.get("qty")) for row in exits)
            entry_price, exit_price = entry_quote / entry_qty, exit_quote / exit_qty
            realized_pnl = sum(number(row.get("realizedPnl")) for row in relevant)
            commission = sum(number(row.get("commission")) for row in relevant)
            net_pnl = realized_pnl - commission
            risk_quote = number(execution.get("initial_risk_quote")) or (
                entry_quote * number(signal.get("risk_pct")) / 100.0
            )
            final_exit_price = number(exits[-1].get("price"), exit_price)
            stop_distance = abs(final_exit_price - number(execution.get("stop_price", signal.get("stop_price"))))
            target_distance = abs(final_exit_price - number(execution.get("target_price", signal.get("target_price"))))
            default_exit_reason = execution.get("forced_exit_reason") or (
                "STOP" if stop_distance <= target_distance else "TARGET"
            )
            if execution.get("full_runner_armed") and net_pnl > 0:
                default_exit_reason = "RUNNER_STOP"
            result = {
                "execution_id": item["id"], "closed_at_ms": max(int(row.get("time") or 0) for row in relevant),
                "mode": self.mode, "symbol": symbol, "direction": item["direction"],
                "entry_price": entry_price, "exit_price": exit_price, "final_exit_price": final_exit_price,
                "quantity": min(entry_qty, exit_qty),
                "realized_pnl": realized_pnl, "commission": commission, "net_pnl": net_pnl,
                "net_pct": net_pnl / entry_quote * 100.0 if entry_quote else 0.0,
                "result_r": net_pnl / risk_quote if risk_quote else 0.0,
                "exit_reason": default_exit_reason,
                "trades": relevant,
            }
            for order in state.get("algo_orders") or []:
                if str(order.get("symbol") or "") == symbol and order.get("algoId"):
                    self.request("DELETE", "/fapi/v1/algoOrder", {
                        "symbol": symbol, "algoId": order["algoId"],
                    })
            completed.append(result)
        return completed

    def _ensure_one_way(self) -> None:
        state = self.request("GET", "/fapi/v1/positionSide/dual")
        if str(state.get("dualSidePosition")).lower() == "true":
            raise RuntimeError("Hedge Mode nao suportado; configure One-way Mode sem posicoes abertas")

    def _configure_symbol(self, symbol: str) -> None:
        self.request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": int(self.settings["leverage"])})
        try:
            self.request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": self.settings["margin_type"]})
        except RuntimeError as exc:
            if "-4046" not in str(exc):  # No need to change margin type.
                raise

    def _market_close(self, symbol: str, side: str, quantity: str) -> Any:
        return self.request("POST", "/fapi/v1/order", {
            "symbol": symbol, "side": side, "type": "MARKET", "quantity": quantity,
            "reduceOnly": "true", "newOrderRespType": "RESULT",
        })

    def _actual_position_entry(self, symbol: str, direction: str, fallback: float) -> tuple[float, str]:
        for _ in range(4):
            try:
                position = next(
                    (row for row in self.positions() if str(row.get("symbol")) == symbol
                     and abs(number(row.get("positionAmt"))) > 0),
                    None,
                )
                if position is not None:
                    amount = number(position.get("positionAmt"))
                    actual = number(position.get("entryPrice"))
                    if actual > 0 and (amount > 0) == (direction == "LONG"):
                        return actual, "POSITION_RISK"
            except Exception:
                pass
            time.sleep(0.10)
        return fallback, "ORDER_RESULT"

    def _cancel_symbol_orders(self, symbol: str) -> None:
        try:
            self.request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})
        except Exception:
            pass
        for order in self.open_algo_orders(symbol):
            if order.get("algoId"):
                try:
                    self.request("DELETE", "/fapi/v1/algoOrder", {
                        "symbol": symbol, "algoId": order["algoId"],
                    })
                except Exception:
                    pass

    def _place_protection(
        self, symbol: str, direction: str, stop: float, target: float, place_target: bool = True,
        working_type: str | None = None,
    ) -> dict[str, Any]:
        long = direction == "LONG"
        close_side = "SELL" if long else "BUY"
        rules = self.symbol_rules(symbol)
        stop_trigger = RiskSizer.trigger_price(stop, rules["tick_size"], round_up=not long)
        target_trigger = RiskSizer.trigger_price(target, rules["tick_size"], round_up=long)
        selected_working_type = str(working_type or self.settings["working_type"])
        stop_order = self.request("POST", "/fapi/v1/algoOrder", {
            "algoType": "CONDITIONAL", "symbol": symbol, "side": close_side, "type": "STOP_MARKET",
            "triggerPrice": format(stop_trigger, "f"), "closePosition": "true",
            "workingType": selected_working_type, "newOrderRespType": "RESULT",
        })
        if not place_target:
            return {
                "stop_algo_id": stop_order.get("algoId"), "take_algo_id": None,
                "stop_price": float(stop_trigger), "target_price": float(target_trigger),
                "runner_target_disabled": True, "working_type": selected_working_type,
            }
        try:
            take_order = self.request("POST", "/fapi/v1/algoOrder", {
                "algoType": "CONDITIONAL", "symbol": symbol, "side": close_side,
                "type": "TAKE_PROFIT_MARKET", "triggerPrice": format(target_trigger, "f"),
                "closePosition": "true", "workingType": selected_working_type,
                "newOrderRespType": "RESULT",
            })
        except Exception:
            if stop_order.get("algoId"):
                self.request("DELETE", "/fapi/v1/algoOrder", {
                    "symbol": symbol, "algoId": stop_order["algoId"],
                })
            raise
        return {
            "stop_algo_id": stop_order.get("algoId"), "take_algo_id": take_order.get("algoId"),
            "stop_price": float(stop_trigger), "target_price": float(target_trigger),
            "working_type": selected_working_type,
        }

    def contract_price(self, symbol: str) -> float:
        ticker = self.request("GET", "/fapi/v1/ticker/price", {"symbol": symbol}, signed=False)
        price = number(ticker.get("price"))
        if price <= 0:
            raise RuntimeError("preco contratual indisponivel")
        return price

    def update_stop(
        self, symbol: str, direction: str, stop_price: float,
        working_type: str | None = None,
    ) -> dict[str, Any]:
        state = self.account_state({symbol})
        position = next((row for row in state["positions"] if row.get("symbol") == symbol), None)
        if position is None:
            raise RuntimeError("posicao ausente para atualizar stop")
        amount = number(position.get("positionAmt"))
        if (amount > 0) != (direction == "LONG"):
            raise RuntimeError("direcao Binance diverge da protecao")
        selected_working_type = str(working_type or self.settings["working_type"]).upper()
        current = (
            self.contract_price(symbol) if selected_working_type == "CONTRACT_PRICE"
            else abs(number(position.get("markPrice")))
        )
        if current <= 0:
            current = self._execution_book(symbol)["mid"]
        if (direction == "LONG" and stop_price >= current) or (direction == "SHORT" and stop_price <= current):
            raise RuntimeError("novo stop de lucro ja foi atravessado pelo mercado")

        old_stops = [
            row for row in self.open_algo_orders(symbol)
            if str(row.get("orderType") or row.get("type") or "").upper() == "STOP_MARKET"
        ]
        long = direction == "LONG"
        rules = self.symbol_rules(symbol)
        trigger = RiskSizer.trigger_price(stop_price, rules["tick_size"], round_up=not long)
        order_params = {
            "algoType": "CONDITIONAL", "symbol": symbol,
            "side": "SELL" if long else "BUY", "type": "STOP_MARKET",
            "triggerPrice": format(trigger, "f"), "closePosition": "true",
            "workingType": selected_working_type, "newOrderRespType": "RESULT",
        }
        # Binance rejects two closePosition STOP_MARKET orders in the same direction (-4130).
        # Cancel the old stop immediately before replacement and restore it if replacement fails.
        old_trigger = next(
            (number(row.get("triggerPrice") or row.get("stopPrice")) for row in old_stops
             if number(row.get("triggerPrice") or row.get("stopPrice")) > 0),
            0.0,
        )
        for row in old_stops:
            old_id = row.get("algoId")
            if old_id:
                self.request("DELETE", "/fapi/v1/algoOrder", {"symbol": symbol, "algoId": old_id})
        try:
            new_stop = self.request("POST", "/fapi/v1/algoOrder", order_params)
        except Exception as replacement_error:
            remaining_stops = [
                row for row in self.open_algo_orders(symbol)
                if str(row.get("orderType") or row.get("type") or "").upper() == "STOP_MARKET"
            ]
            if remaining_stops:
                raise RuntimeError(f"troca de stop recusada; protecao anterior permaneceu: {replacement_error}")
            if old_trigger > 0:
                restore_params = {**order_params, "triggerPrice": format(old_trigger, "f")}
                try:
                    self.request("POST", "/fapi/v1/algoOrder", restore_params)
                except Exception as restore_error:
                    self.close_symbol(symbol)
                    raise RuntimeError(
                        f"falha ao trocar e restaurar stop; posicao encerrada: {restore_error}"
                    ) from replacement_error
            raise
        new_id = new_stop.get("algoId")
        return {
            "ok": True, "symbol": symbol, "stop_algo_id": new_id,
            "stop_price": float(trigger), "working_type": selected_working_type,
        }

    def disable_take_profit(self, symbol: str) -> dict[str, Any]:
        """Remove only take-profit orders; the stop and the full position remain untouched."""
        cancelled = []
        for row in self.open_algo_orders(symbol):
            order_type = str(row.get("orderType") or row.get("type") or "").upper()
            algo_id = row.get("algoId")
            if order_type not in {"TAKE_PROFIT", "TAKE_PROFIT_MARKET"} or not algo_id:
                continue
            self.request("DELETE", "/fapi/v1/algoOrder", {"symbol": symbol, "algoId": algo_id})
            cancelled.append(algo_id)
        return {
            "ok": True, "symbol": symbol, "runner_target_disabled": True,
            "cancelled_take_algo_ids": cancelled, "take_algo_id": None,
        }

    def _execution_book(self, symbol: str) -> dict[str, float]:
        book = self.request("GET", "/fapi/v1/ticker/bookTicker", {"symbol": symbol}, signed=False)
        bid = number(book.get("bidPrice"))
        ask = number(book.get("askPrice"))
        mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else 0.0
        if mid <= 0 or ask < bid:
            raise RuntimeError("book executavel invalido")
        return {"bid": bid, "ask": ask, "mid": mid, "spread_pct": (ask - bid) / mid * 100.0}

    def _execution_depth(self, symbol: str, side: str, quantity: float, levels: int) -> dict[str, float]:
        """Estimate the VWAP and impact of the exact market quantity before sending it."""
        depth = self.request(
            "GET", "/fapi/v1/depth",
            {"symbol": symbol, "limit": max(5, min(int(levels), 100))},
            signed=False,
        )
        rows = depth.get("asks") if str(side).upper() == "BUY" else depth.get("bids")
        remaining, quote, filled = float(quantity), 0.0, 0.0
        best = number(rows[0][0]) if rows else 0.0
        for raw_price, raw_qty in rows or []:
            price, available = number(raw_price), number(raw_qty)
            take = min(remaining, available)
            quote += take * price
            filled += take
            remaining -= take
            if remaining <= 1e-12:
                break
        if best <= 0 or filled <= 0 or remaining > max(1e-12, float(quantity) * 1e-6):
            raise RuntimeError("profundidade insuficiente para a ordem padrao")
        vwap = quote / filled
        impact = (
            (vwap - best) / best * 100.0
            if str(side).upper() == "BUY" else (best - vwap) / best * 100.0
        )
        return {
            "depth_best_price": best,
            "depth_vwap_price": vwap,
            "depth_impact_pct": max(0.0, impact),
            "depth_quantity": filled,
        }

    def close_symbol(self, symbol: str) -> dict[str, Any]:
        state = self.account_state({symbol})
        position = next((row for row in state["positions"] if row.get("symbol") == symbol), None)
        if position is None:
            self._cancel_symbol_orders(symbol)
            return {"ok": True, "symbol": symbol, "closed": False}
        amount = number(position.get("positionAmt"))
        rules = self.symbol_rules(symbol)
        qty = RiskSizer.floor_step(abs(amount), rules["step_size"])
        result = self._market_close(symbol, "SELL" if amount > 0 else "BUY", format(qty, "f"))
        self._cancel_symbol_orders(symbol)
        return {"ok": True, "symbol": symbol, "closed": True, "order_id": result.get("orderId")}

    def take_partial(
        self, symbol: str, direction: str, fraction: float, stop_price: float, target_price: float,
        fallback_stop_price: float | None = None,
    ) -> dict[str, Any]:
        book = self._execution_book(symbol)
        max_spread = float(self.settings.get(
            "max_management_execution_spread_pct",
            self.settings.get("max_execution_spread_pct", 0.15),
        ))
        if book["spread_pct"] > max_spread:
            return {
                "ok": False, "deferred": True, "symbol": symbol,
                "reason": "execution_spread_too_wide",
                "execution_spread_pct": book["spread_pct"],
                "max_management_execution_spread_pct": max_spread,
            }
        state = self.account_state({symbol})
        position = next((row for row in state["positions"] if row.get("symbol") == symbol), None)
        if position is None:
            raise RuntimeError("posicao ausente para parcial")
        amount = number(position.get("positionAmt"))
        if (amount > 0) != (direction == "LONG"):
            raise RuntimeError("direcao Binance diverge da campanha")
        rules = self.symbol_rules(symbol)
        qty = RiskSizer.floor_step(abs(amount) * fraction, rules["step_size"])
        remaining = RiskSizer.floor_step(abs(amount) - float(qty), rules["step_size"])
        if qty < Decimal(str(rules["min_qty"])) or remaining < Decimal(str(rules["min_qty"])):
            raise RuntimeError("quantidade insuficiente para parcial segura")
        self._cancel_symbol_orders(symbol)
        close = self._market_close(symbol, "SELL" if amount > 0 else "BUY", format(qty, "f"))
        refreshed = self.account_state({symbol})
        remaining_position = next(
            (row for row in refreshed["positions"] if row.get("symbol") == symbol), None,
        )
        if remaining_position is None:
            raise RuntimeError("posicao remanescente ausente apos parcial")
        mark = abs(number(remaining_position.get("markPrice"))) or book["mid"]
        requested_stop = float(stop_price)
        fallback_stop = float(fallback_stop_price or 0.0)

        def valid_runner_stop(value: float) -> bool:
            return value > 0 and (value < mark if direction == "LONG" else value > mark)

        runner_stop_adjusted = False
        if not valid_runner_stop(requested_stop):
            if valid_runner_stop(fallback_stop):
                stop_price = fallback_stop
                runner_stop_adjusted = True
            else:
                self.close_symbol(symbol)
                raise RuntimeError(
                    "stop do runner invalido apos fill da parcial; remanescente encerrado com seguranca"
                )
        try:
            protection = self._place_protection(symbol, direction, stop_price, target_price)
        except Exception:
            self.close_symbol(symbol)
            raise
        return {
            "ok": True, "symbol": symbol, "quantity_closed": format(qty, "f"),
            "partial_order_id": close.get("orderId"),
            "execution_spread_pct": book["spread_pct"],
            "requested_runner_stop_price": requested_stop,
            "runner_stop_adjusted": runner_stop_adjusted,
            **protection,
        }

    def execute(self, signal: dict[str, Any]) -> dict[str, Any]:
        symbol, direction = signal["symbol"], signal["direction"]
        long = direction == "LONG"
        entry_side, close_side = ("BUY", "SELL") if long else ("SELL", "BUY")
        self._ensure_one_way()
        state = self.account_state()
        if any(row.get("symbol") == symbol for row in state["positions"]):
            raise RuntimeError("posicao ja aberta no simbolo")
        position_limit = int(
            self.settings.get("testnet_max_open_positions", self.settings["max_open_positions"])
            if self.mode == "TESTNET" else self.settings["max_open_positions"]
        )
        if position_limit > 0 and len(state["positions"]) >= position_limit:
            raise RuntimeError("limite de posicoes abertas atingido")
        book = self._execution_book(symbol)
        spread_key = (
            "testnet_max_entry_execution_spread_pct"
            if self.mode == "TESTNET" else "max_entry_execution_spread_pct"
        )
        max_spread = float(self.settings.get(
            spread_key,
            self.settings.get(
                "max_entry_execution_spread_pct",
                self.settings.get("max_execution_spread_pct", 0.15),
            ),
        ))
        real_market_admitted = (
            self.mode == "TESTNET"
            and bool(self.settings.get("testnet_require_real_market_admission", False))
        )
        if not real_market_admitted and book["spread_pct"] > max_spread:
            raise RuntimeError(
                f"spread executavel excessivo: {book['spread_pct']:.4f}% > {max_spread:.4f}%"
            )
        executable = book["ask"] if long else book["bid"]
        reference = number(signal["entry_price"])
        signed_drift = (executable - reference) / reference * 100.0 if reference else 999.0
        adverse_drift = signed_drift if long else -signed_drift
        signal_stop = number(signal["stop_price"])
        signal_target = number(signal["target_price"])
        signal_risk = abs(reference - signal_stop)
        signal_reward = abs(signal_target - reference)
        signal_rr = signal_reward / signal_risk if signal_risk > 0 else 0.0
        signal_risk_pct = signal_risk / reference * 100.0 if reference else 0.0
        rebase_testnet = self.mode == "TESTNET" and bool(self.settings.get("testnet_rebase_market_basis", False))
        if rebase_testnet:
            max_basis = float(self.settings.get("testnet_max_market_basis_pct", 0.50))
            if abs(signed_drift) > max_basis:
                raise RuntimeError(f"basis Testnet excessiva: {signed_drift:.4f}%")
            risk_distance = executable * signal_risk_pct / 100.0
            stop = executable - risk_distance if long else executable + risk_distance
            target = executable + risk_distance * signal_rr if long else executable - risk_distance * signal_rr
        else:
            if adverse_drift > float(self.settings["max_entry_drift_pct"]):
                raise RuntimeError(f"preco stale: drift adverso {adverse_drift:.4f}%")
            if -adverse_drift > float(self.settings.get("max_favorable_entry_drift_pct", 0.20)):
                raise RuntimeError(f"preco divergente: drift favoravel {-adverse_drift:.4f}%")
            stop, target = signal_stop, signal_target
        expected_risk = executable - stop if long else stop - executable
        expected_reward = target - executable if long else executable - target
        expected_rr = expected_reward / expected_risk if expected_risk > 0 else 0.0
        commission = self.commission_rates(symbol)
        taker_rate = commission["taker"]
        expected_fee_distance = executable * taker_rate + target * taker_rate
        expected_net_risk = expected_risk + expected_fee_distance
        expected_net_reward = expected_reward - expected_fee_distance
        expected_net_rr = expected_net_reward / expected_net_risk if expected_net_risk > 0 else 0.0
        setup = str(signal.get("setup") or "")
        working_type = str(
            (self.settings.get("strategy_working_type_overrides") or {}).get(
                setup, self.settings["working_type"],
            )
        )
        min_executable_rr = float(
            (self.settings.get("strategy_min_executable_rr_overrides") or {}).get(
                setup, self.settings.get("min_executable_rr", 1.5),
            )
        )
        if expected_risk <= 0 or expected_net_reward <= 0 or expected_net_rr < min_executable_rr:
            raise RuntimeError(
                f"RR liquido executavel insuficiente: {expected_net_rr:.2f}R "
                f"(bruto {expected_rr:.2f}R, taker {taker_rate * 100:.3f}% por lado)"
            )
        account = self.account()
        wallet = number(account.get("totalWalletBalance"))
        fixed_margin = float(self.settings.get("fixed_margin_usdt", 0.0))
        leverage = int(self.settings["leverage"])
        fixed_tolerance_pct = float(self.settings.get("fixed_margin_tolerance_pct", 2.0))
        margin_cap = wallet * float(self.settings["max_margin_pct"]) / 100.0
        if fixed_margin > 0 and margin_cap < fixed_margin * (1.0 - fixed_tolerance_pct / 100.0):
            raise RuntimeError(
                f"margem disponivel pelo limite insuficiente para padrao de {fixed_margin:.2f} USDT"
            )
        raw_qty = RiskSizer.quantity(
            wallet, executable, stop, leverage,
            float(signal.get("account_risk_pct", self.settings["account_risk_pct"])),
            float(self.settings["max_margin_pct"]),
            taker_rate * 2.0,
            fixed_margin,
        )
        rules = self.symbol_rules(symbol)
        capped_qty = min(raw_qty, rules["max_qty"])
        qty = (
            RiskSizer.nearest_step(capped_qty, rules["step_size"])
            if fixed_margin > 0 else RiskSizer.floor_step(capped_qty, rules["step_size"])
        )
        if float(qty) > float(rules["max_qty"]):
            qty = RiskSizer.floor_step(rules["max_qty"], rules["step_size"])
        if qty < Decimal(str(rules["min_qty"])) or float(qty) * executable < rules["min_notional"]:
            raise RuntimeError("quantidade abaixo do minimo da Binance")
        effective_margin = float(qty) * executable / leverage
        if fixed_margin > 0:
            deviation_pct = abs(effective_margin - fixed_margin) / fixed_margin * 100.0
            if deviation_pct > fixed_tolerance_pct:
                raise RuntimeError(
                    f"simbolo nao comporta margem padrao: {effective_margin:.2f} USDT "
                    f"({deviation_pct:.2f}% de desvio; exigido {fixed_margin:.2f} USDT)"
                )
        depth_result: dict[str, float] = {}
        depth_cfg = self.settings.get("depth_filter") or {}
        if bool(depth_cfg.get("enabled", False)) and not real_market_admitted:
            depth_result = self._execution_depth(
                symbol, entry_side, float(qty), int(depth_cfg.get("levels", 20)),
            )
            impact_key = (
                "testnet_max_entry_impact_pct" if self.mode == "TESTNET"
                else "max_entry_impact_pct"
            )
            max_impact = float(depth_cfg.get(
                impact_key, depth_cfg.get("max_entry_impact_pct", 0.20),
            ))
            if depth_result["depth_impact_pct"] > max_impact:
                raise RuntimeError(
                    "impacto de profundidade excessivo: "
                    f"{depth_result['depth_impact_pct']:.4f}% > {max_impact:.4f}%"
                )
        qty_text = format(qty, "f")
        self._configure_symbol(symbol)
        open_order = self.request("POST", "/fapi/v1/order", {
            "symbol": symbol, "side": entry_side, "type": "MARKET", "quantity": qty_text,
            "newOrderRespType": "RESULT",
        })
        reported_fill = number(open_order.get("avgPrice")) or executable
        fill, fill_source = self._actual_position_entry(symbol, direction, reported_fill)
        if rebase_testnet:
            risk_distance = fill * signal_risk_pct / 100.0
            stop = fill - risk_distance if long else fill + risk_distance
            target = fill + risk_distance * signal_rr if long else fill - risk_distance * signal_rr

        def safety_close(reason: str, error: str) -> dict[str, Any]:
            close_order = self._market_close(symbol, close_side, qty_text)
            risk_distance = abs(fill - stop)
            risk_quote = float(qty) * (risk_distance + fill * taker_rate * 2.0)
            return {
                "ok": True, "position_open": False, "safety_closed": True,
                "mode": self.mode, "symbol": symbol, "direction": direction,
                "quantity": qty_text, "fill_price": fill,
                "reported_fill_price": reported_fill, "fill_price_source": fill_source,
                "open_order_id": open_order.get("orderId"),
                "safety_close_order_id": close_order.get("orderId"),
                "stop_price": stop, "target_price": target,
                "initial_stop_price": stop, "initial_target_price": target,
                "initial_risk_distance": risk_distance,
                "initial_risk_quote": risk_quote,
                "forced_exit_reason": "SAFETY_CLOSE",
                "reason": reason, "error": error,
                "account_risk_pct": float(signal.get("account_risk_pct", self.settings["account_risk_pct"])),
                "fixed_margin_usdt": float(self.settings.get("fixed_margin_usdt", 0.0)),
                "effective_entry_margin_usdt": float(qty) * fill / leverage,
                "leverage": int(self.settings["leverage"]),
                "entry_drift_pct": signed_drift,
                "maker_commission_rate": commission["maker"], "taker_commission_rate": taker_rate,
                "testnet_levels_rebased": rebase_testnet,
                "execution_spread_pct": book["spread_pct"],
                **depth_result,
            }
        if (long and stop >= fill) or (not long and stop <= fill):
            return safety_close("fill_invalidated_stop", "fill tornou o stop invalido")
        actual_risk = fill - stop if long else stop - fill
        actual_reward = target - fill if long else fill - target
        actual_rr = actual_reward / actual_risk if actual_risk > 0 else 0.0
        actual_fee_distance = fill * taker_rate + target * taker_rate
        actual_net_risk = actual_risk + actual_fee_distance
        actual_net_reward = actual_reward - actual_fee_distance
        actual_net_rr = actual_net_reward / actual_net_risk if actual_net_risk > 0 else 0.0
        if actual_net_reward <= 0 or actual_net_rr < min_executable_rr:
            return safety_close(
                "fill_degraded_net_rr",
                f"fill degradou RR liquido para {actual_net_rr:.2f}R (bruto {actual_rr:.2f}R)",
            )
        try:
            protection = self._place_protection(
                symbol, direction, stop, target,
                place_target=not bool(signal.get("full_position_runner")),
                working_type=working_type,
            )
        except Exception as exc:
            return safety_close("protection_rejected", str(exc))
        return {
            "ok": True, "mode": self.mode, "symbol": symbol, "direction": direction,
            "quantity": qty_text, "fill_price": fill, "reported_fill_price": reported_fill,
            "fill_price_source": fill_source, "open_order_id": open_order.get("orderId"),
            **protection,
            "initial_stop_price": float(protection["stop_price"]),
            "initial_target_price": float(protection["target_price"]),
            "initial_risk_distance": actual_risk,
            "initial_risk_quote": float(qty) * actual_net_risk,
            "account_risk_pct": float(signal.get("account_risk_pct", self.settings["account_risk_pct"])),
            "fixed_margin_usdt": float(self.settings.get("fixed_margin_usdt", 0.0)),
            "effective_entry_margin_usdt": float(qty) * fill / leverage,
            "leverage": leverage,
            "entry_drift_pct": signed_drift, "executable_rr": expected_rr, "fill_rr": actual_rr,
            "executable_net_rr": expected_net_rr, "fill_net_rr": actual_net_rr,
            "maker_commission_rate": commission["maker"], "taker_commission_rate": taker_rate,
            "testnet_levels_rebased": rebase_testnet,
            "execution_spread_pct": book["spread_pct"],
            **depth_result,
            "full_position_runner": bool(signal.get("full_position_runner")),
        }


class ExecutionRouter:
    def __init__(self, config: dict[str, Any], root: Path):
        self.mode = str(config["mode"]).upper()
        self.settings = config["execution"]
        self.broker = None if self.mode == "SHADOW" else BinanceFuturesBroker(self.mode, self.settings, root)
        self.exit_reason_overrides: dict[str, str] = {}

    def execute(self, signal: dict[str, Any], journal: Any, max_concurrent_shadow: int) -> dict[str, Any]:
        if not bool(self.settings.get("new_entries_enabled", True)):
            return {
                "ok": False,
                "mode": self.mode,
                "reason": "novas_entradas_pausadas",
                "error": "Novas entradas pausadas pelo disjuntor; monitoramento e protecoes continuam ativos.",
            }
        if self.mode == "SHADOW":
            ok, reason = journal.record_signal(signal, int(max_concurrent_shadow))
            return {"ok": ok, "mode": "SHADOW", "reason": reason}
        return self.broker.execute(signal)

    def account_state(self, monitored_symbols: set[str] | None = None) -> dict[str, Any]:
        return {"mode": "SHADOW", "positions": [], "orders": [], "algo_orders": []} if self.broker is None else self.broker.account_state(monitored_symbols)

    def supported_symbols(self) -> set[str] | None:
        return None if self.broker is None else self.broker.supported_symbols()

    def standard_margin_symbols(self) -> set[str] | None:
        return None if self.broker is None else self.broker.standard_margin_symbols()

    def reconcile(self, journal: Any, state: dict[str, Any]) -> list[dict[str, Any]]:
        if self.broker is None:
            return []
        completed = self.broker.reconcile(journal.pending_executions(), state)
        for result in completed:
            result["exit_reason"] = self.exit_reason_overrides.pop(result["symbol"], result["exit_reason"])
            journal.record_execution_result(result)
        return completed

    def close_symbol(self, symbol: str, reason: str) -> dict[str, Any]:
        if self.broker is None:
            return {"ok": True, "mode": "SHADOW", "symbol": symbol, "closed": True}
        result = self.broker.close_symbol(symbol)
        if result.get("closed"):
            self.exit_reason_overrides[symbol] = reason
        return result

    def take_partial(
        self, symbol: str, direction: str, fraction: float, stop_price: float, target_price: float,
        fallback_stop_price: float | None = None,
    ) -> dict[str, Any]:
        if self.broker is None:
            return {"ok": True, "mode": "SHADOW", "symbol": symbol}
        return self.broker.take_partial(
            symbol, direction, fraction, stop_price, target_price, fallback_stop_price,
        )

    def contract_price(self, symbol: str) -> float:
        if self.broker is None:
            return 0.0
        return self.broker.contract_price(symbol)

    def update_stop(
        self, symbol: str, direction: str, stop_price: float,
        working_type: str | None = None,
    ) -> dict[str, Any]:
        if self.broker is None:
            return {"ok": True, "mode": "SHADOW", "symbol": symbol, "stop_price": stop_price}
        return self.broker.update_stop(symbol, direction, stop_price, working_type)

    def normalized_stop_price(self, symbol: str, direction: str, stop_price: float) -> float:
        """Return the exchange tick that update_stop would actually submit."""
        if self.broker is None:
            return float(stop_price)
        rules = self.broker.symbol_rules(symbol)
        return float(RiskSizer.trigger_price(
            float(stop_price), rules["tick_size"], round_up=str(direction).upper() != "LONG",
        ))

    def disable_take_profit(self, symbol: str) -> dict[str, Any]:
        if self.broker is None:
            return {
                "ok": True, "mode": "SHADOW", "symbol": symbol,
                "runner_target_disabled": True, "cancelled_take_algo_ids": [],
            }
        return self.broker.disable_take_profit(symbol)

    def close_all(self) -> dict[str, Any]:
        if self.broker is None:
            return {"ok": True, "mode": "SHADOW", "actions": [], "errors": [], "remaining": {}}
        return self.broker.close_all()
