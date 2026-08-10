from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
import tempfile
import time
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Query
from fastapi.responses import HTMLResponse
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from engine.config import ROOT, load_config
from engine.ledger_export import LEDGER_SPECS, export_filename, export_ledger_zip
from engine.service import TradingService


config = load_config()
service = TradingService(config, ROOT)


@asynccontextmanager
async def lifespan(_: FastAPI):
    service.start()
    yield
    service.stop()


app = FastAPI(title=config["app_name"], lifespan=lifespan)
LEGACY_UI = Path(r"C:\Users\BOT1\Documents\v10_legacy_source_20260728_0948")
app.mount("/static", StaticFiles(directory=LEGACY_UI / "static"), name="static")


def dashboard_config() -> dict[str, Any]:
    mode = "BINANCE_TESTNET" if config["mode"] == "TESTNET" else "BINANCE_REAL" if config["mode"] == "REAL" else "SHADOW"
    execution = config["execution"]
    strategy = config["strategy"]
    return {
        "app_name": config["app_name"], "mode": mode,
        "new_entries_enabled": bool(execution.get("new_entries_enabled", True)),
        "min_quote_volume_24h": config["min_quote_volume_24h"],
        "max_symbols_light_scan": 0, "max_symbols_heavy_scan": 0,
        "refresh_seconds": config["scan_seconds"],
        "position_refresh_seconds": config.get("live_account_refresh_seconds", 2),
        "max_open_positions": execution["max_open_positions"],
        "fee_pct_per_side": strategy["fee_pct_per_side"],
        "binance_pnl_reference_balance_usdt": config.get("binance_pnl_reference_balance_usdt", 0),
        "chart_interval": "5m", "chart_limit": 120, "mini_charts_enabled": True,
        "chart_live_refresh_seconds": 1, "chart_server_refresh_seconds": 15,
        "chart_crosshair_enabled": True, "chart_vertical_zoom_enabled": True,
        "chart_show_zones": True, "chart_show_volume": False, "chart_show_indicators": False,
        "show_position_age_on_card": True, "closed_trades_show_close_time": True,
        "binance_staged_take_profit_enabled": False,
        "entry": {"cooldown_minutes_by_symbol": strategy["cooldown_minutes"]},
        "score_weights": {},
        "bollinger_range_scalp_enabled": False,
        "strategies": {"bollinger_range_scalp": {"enabled": False}},
        "real_execution": {
            "default_leverage": execution["leverage"],
            "default_margin_usdt": execution.get("fixed_margin_usdt", 0),
            "max_open_positions": execution["max_open_positions"], "real_password_unlocked": False,
        },
    }


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _ui_positions(status: dict[str, Any]) -> list[dict[str, Any]]:
    state = status["execution_state"]
    orders = list(state.get("orders") or []) + list(state.get("algo_orders") or [])
    ledger_open = {row["symbol"]: row for row in service.journal.open_execution_ledger()}
    result = []
    for raw in state.get("positions") or []:
        symbol = str(raw.get("symbol") or "")
        amount, entry, mark = _number(raw.get("positionAmt")), _number(raw.get("entryPrice")), _number(raw.get("markPrice"))
        current = _number(raw.get("contractPrice")) or mark
        quantity, pnl = abs(amount), _number(raw.get("unRealizedProfit", raw.get("unrealizedProfit")))
        leverage = _number(raw.get("leverage")) or _number(config["execution"]["leverage"])
        margin = _number(raw.get("isolatedWallet")) or _number(raw.get("isolatedMargin")) or (entry * quantity / leverage if leverage else 0)
        linked = [row for row in orders if str(row.get("symbol") or "") == symbol]
        normalized_orders = []
        stop_price, take_price = 0.0, 0.0
        direction = "LONG" if amount > 0 else "SHORT"
        for order in linked:
            trigger = _number(order.get("triggerPrice", order.get("stopPrice")))
            normalized = {**order, "trigger_price": trigger, "source": status["mode"]}
            normalized_orders.append(normalized)
            order_type = str(order.get("orderType") or order.get("type") or "").upper()
            if "TAKE_PROFIT" in order_type:
                take_price = trigger
            elif "STOP" in order_type:
                stop_price = trigger
        opened = ledger_open.get(symbol, {})
        fee_rate = _number(opened.get("taker_commission_rate")) or (
            _number(config["execution"].get("fallback_taker_fee_pct", 0.05)) / 100.0
        )
        estimated_fees = (entry + current) * quantity * fee_rate
        net_pnl_if_closed = pnl - estimated_fees
        opened_at_ms = int(opened.get("opened_at_ms") or raw.get("updateTime") or time.time() * 1000)
        evidence = opened.get("evidence") if isinstance(opened.get("evidence"), dict) else {}
        entry_evidence = evidence.get("entry") if isinstance(evidence.get("entry"), dict) else {}
        campaign_action = str(opened.get("campaign_action") or "ENTRY")
        campaign_id = str(opened.get("campaign_id") or "")
        entry_reason = f"Campanha {campaign_action}"
        if entry_evidence:
            taker_delta = (_number(entry_evidence.get("taker_buy_sell_ratio")) - 1.0) * 100.0
            entry_reason += " | " + (
                f"OI 15m {_number(entry_evidence.get('oi_change_15m_pct')):.2f}% | "
                f"Volume {_number(entry_evidence.get('volume_ratio')):.2f}x | "
                f"LSR contas globais {_number(entry_evidence.get('global_lsr')):.2f} | "
                f"Taker delta {taker_delta:.2f}% | "
                f"Funding {_number(entry_evidence.get('funding_rate_pct')):.4f}% | "
                f"Spread {_number(entry_evidence.get('spread_pct')):.3f}%"
            )
        result.append({
            **opened, "source": "BINANCE " + status["mode"], "symbol": symbol, "direction": direction,
            "entry_price": entry, "current_price": current, "mark_price": mark,
            "price_basis": raw.get("managementPriceType") or "MARK_PRICE",
            "quantity": quantity, "initial_quantity": quantity,
            "notional": abs(_number(raw.get("notional"))) or entry * quantity,
            "leverage": leverage, "margin_used": margin, "initial_margin_usdt": margin,
            "pnl_live": net_pnl_if_closed,
            "pnl_gross_live": pnl,
            "estimated_round_trip_fees": estimated_fees,
            "roe_live": net_pnl_if_closed / margin * 100 if margin else 0,
            "stop_price": stop_price or opened.get("stop_price", 0),
            "take_price": take_price or opened.get("take_price", 0),
            "initial_stop_price": opened.get("initial_stop_price", stop_price),
            "initial_take_price": opened.get("initial_take_price", take_price),
            "setup": opened.get("setup", "DUMP_REGIME_V2"), "score_entry": 0,
            "campaign_action": campaign_action, "campaign_id": campaign_id,
            "opened_at_ms": opened_at_ms,
            "age_seconds": max(0, (time.time() * 1000 - opened_at_ms) / 1000),
            "entry_reason": entry_reason,
            "tp_sl_mode": f"CAMPAIGN_{campaign_action}", "binance_orders": normalized_orders,
            "binance_truth": {
                "stop": {"price": stop_price} if stop_price else {},
                "tps": [{"stage": 1, "price": take_price, "quantity": quantity, "source": "BINANCE"}] if take_price else [],
            },
        })
    return result


def _legacy_state_payload() -> dict[str, Any]:
    status = service.status()
    ledger = service.journal.execution_ledger()
    account = dict(status.get("account") or {})
    account["ok"] = bool(account)
    closed = ledger["closed"]
    default_leverage = _number(config["execution"].get("leverage"))
    for row in closed:
        quantity = _number(row.get("quantity"))
        entry_price = _number(row.get("entry_price"))
        margin = entry_price * quantity / default_leverage if default_leverage else 0.0
        row["leverage"] = default_leverage
        row["initial_margin_usdt"] = margin
        row["pnl_pct_on_margin"] = _number(row.get("pnl_net")) / margin * 100.0 if margin else 0.0
    net = sum(_number(row.get("pnl_net")) for row in closed)
    account["income_summary"] = {"strategy_ledger_net": net, "funding_fee": 0.0}
    recent_events = service.journal.recent_events(200)
    counts: dict[str, int] = {}
    for event in recent_events:
        event_type = str(event.get("type") or "")
        counts[event_type] = counts.get(event_type, 0) + 1
    now_label = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    ranking = []
    block_labels = {
        "price_impulse": "impulso insuficiente", "breakout": "sem rompimento",
        "oi_expansion": "OI sem expansão", "volume": "volume insuficiente",
        "taker": "taker não confirma", "crowding": "LSR congestionado",
        "funding": "funding extremo", "spread": "spread alto",
        "taker_not_climax": "taker em clímax",
        "compression": "sem compressão", "range_compact": "range muito amplo",
        "near_boundary": "longe da fronteira", "oi_buildup": "OI sem construção",
        "volume_alive": "volume ainda fraco", "taker_balanced": "taker desequilibrado",
        "fresh_observation": "aguardando nova leitura", "not_chasing": "preço esticado",
        "oi_holds": "OI ainda não acelerou", "volume_accelerates": "volume ainda não acelerou",
        "taker_confirms": "taker ainda não confirma", "lsr_not_deteriorating": "LSR deteriorando",
        "price_acceleration": "preço ainda não acelerou", "oi_acceleration": "OI ainda não acelerou",
        "breakout_hold": "rompimento não sustentado", "not_overextended": "preço esticado",
        "price_accelerates": "preço não acelerou desde o pré-armado",
        "directional_impulse": "impulso atual contrário à direção",
        "price_oi_efficiency": "OI subiu sem resposta do preço",
        "compression_valid": "compressão já se desfez",
        "fresh_oi": "aguardando novo fechamento de OI",
        "price_confirmation": "preço ainda não confirmou",
        "volume_climax": "volume sem clímax",
        "positioning_extreme": "LSR/taker ainda sem excesso",
        "price_reversal": "aguardando reversão do extremo",
        "fresh_closed_candle": "aguardando candle 5m fechado",
        "meaningful_retracement": "retração ainda pequena dentro do squeeze",
        "structure_reverses": "estrutura ainda não virou",
        "oi_exhausts": "OI 5m ainda expandindo",
        "taker_flips": "taker ainda não virou",
        "price_flow_confirms": "agressão absorvida sem resposta do preço",
        "staircase": "aguardando candles direcionais em escada",
        "impulse_window": "impulso fora da janela de entrada",
        "oi_positive": "OI ainda não ficou positivo",
        "oi_accelerates": "OI ainda não acelerou",
        "volume_present": "volume ainda insuficiente",
        "taker_directional": "taker ainda não está direcional",
        "lsr_not_worsening": "LSR deteriorando rápido",
        "fresh_micro_candle": "aguardando novo candle de 1m",
        "micro_reclaim": "microestrutura ainda não retomou",
        "reclaim_distance": "retomada de 1m ainda pequena",
        "three_candle_turn": "aguardando terceira confirmação de 1m",
        "micro_volume_accelerates": "volume de 1m ainda não acelerou",
        "oi_transition": "OI ainda não confirmou a transição",
        "taker_turns": "taker ainda não virou",
        "positioning_allows": "LSR ainda desfavorável",
        "fresh_reclaim_candle": "aguardando novo candle de reclaim",
        "structure_reclaims": "pullback ainda não retomou a estrutura",
        "lsr_bias": "LSR ainda não inclina para baixo",
        "breakout_close": "candle ainda não fechou acima da fronteira",
        "price_holds": "preço voltou para dentro da acumulação",
        "oi_persists": "OI não sustenta a construção",
        "oi_not_decelerating": "OI desacelerando no rompimento",
        "lsr_falls": "LSR não está caindo no rompimento",
        "volume_confirms": "volume não confirma o rompimento",
        "fresh_confirmation_candle": "aguardando candle posterior ao rompimento",
        "sustain_or_retest": "sem sustentação ou reteste válido",
        "oi_not_contracting": "OI contraiu durante a confirmação",
        "lsr_below_arm": "LSR voltou acima do nível armado",
        "lsr_not_reversing": "LSR reverteu contra a campanha",
        "taker_supports": "taker não sustenta a compra",
        "not_extended": "entrada já ficou esticada",
        "extreme_impulse": "movimento ainda não é extremo",
        "outside_structure": "preço ainda não saiu da estrutura",
        "fresh_window": "aguardando maturação do extremo",
        "choch": "CHoCH ainda não confirmado",
        "oi_15m_deteriorates": "OI 15m ainda sustenta o movimento original",
        "oi_5m_not_expanding": "OI 5m ainda expande",
        "lsr_unwinds": "LSR ainda não desmonta o excesso",
        "fresh_hold_candle": "aguardando candle posterior ao CHoCH",
        "structure_holds": "nova estrutura ainda não sustentou",
        "oi_deterioration_persists": "deterioração do OI não persistiu",
        "taker_holds": "taker não sustenta a reversão",
        "ema_direction": "preço ainda do lado contrário da EMA21",
        "ema_slope": "inclinação da EMA21 ainda fraca",
        "trend_strength": "ADX fora da janela direcional",
        "oi_builds": "OI ainda não acompanha a tendência",
        "lsr_not_chasing": "LSR acelerando junto com a multidão",
        "meaningful_pullback": "correção ainda insuficiente",
        "trend_not_broken": "correção perdeu a EMA21",
        "oi_not_flushed": "OI desmontou durante a correção",
        "fresh_reclaim_candle": "aguardando candle posterior ao pullback",
        "structure_reclaims": "preço ainda não retomou a estrutura",
        "directional_close": "candle de retomada ainda sem direção",
        "ema_holds": "EMA21 não foi recuperada",
        "ema_slope_holds": "inclinação da EMA21 se perdeu",
        "trend_alive": "ADX perdeu força",
        "oi_returns": "OI ainda não voltou na retomada",
        "taker_returns": "taker ainda não voltou na direção",
    }
    block_labels.update({
        "sharp_5m_dump": "aguardando queda forte de 5m",
        "causal_silence": "aguardando 15 minutos sem novo dump",
        "no_new_dump": "novo dump reiniciou a espera",
        "oi_building": "OI ainda nao voltou a construir",
        "lsr_falling": "LSR ainda nao esta caindo",
        "taker_buying": "taker comprador ainda nao confirmou",
        "range_width": "largura do range fora da janela",
        "range_defined": "bordas ainda nao definidas",
        "adx_allows": "ADX indica mercado direcional",
        "oi_not_directional": "OI ja ficou direcional",
        "volume_not_climax": "volume em climax",
        "volume_expands": "volume ainda nao confirmou o rompimento",
        "oi_builds": "OI ainda nao confirmou o rompimento",
        "taker_directional": "taker ainda nao acompanha o rompimento",
    })
    for row in status["ranking"]:
        market = row["market"]
        blocked = [block_labels.get(name, name) for name in row["blocked_by"]]
        phase = {
            "BLOCKED": "BLOQUEADO", "ARMED": "ARMADO", "PULLBACK": "RETESTE",
            "PRE_ARMED": "PRÉ-ARMADO", "SQUEEZE_ARMED": "SQUEEZE ARMADO",
            "ACCUMULATION_ARMED": "ACUMULAÇÃO ARMADA", "BREAKOUT_DETECTED": "ROMPIMENTO DETECTADO",
            "EXTREME_ARMED": "EXTREMO ARMADO", "CHOCH_DETECTED": "CHOCH DETECTADO",
            "MOMENTUM_ARMED": "MOMENTUM ARMADO", "MOMENTUM_PULLBACK": "PULLBACK 1M",
            "MICRO_ARMED": "MICRO ARMADO", "MICRO_PULLBACK": "MICRO PULLBACK",
            "TREND_ARMED": "TENDÊNCIA ARMADA", "TREND_PULLBACK": "CORREÇÃO DA TENDÊNCIA",
            "DUMP_ARMED": "DUMP ARMADO",
            "BORDER_ARMED": "BORDAS ARMADAS",
            "BORDER_BREAKOUT": "ROMPIMENTO DA BORDA",
        }.get(row["phase"], row["phase"])
        next_step = blocked
        if phase == "ARMADO":
            next_step = ["aguardando nova janela com aceleração de preço e OI"]
        elif phase == "RETESTE":
            next_step = ["aguardando retomada confirmada"]
        elif phase == "SQUEEZE ARMADO":
            next_step = ["aguardando reversão, virada do taker e perda de aceleração do OI"]
        elif phase == "MOMENTUM ARMADO":
            next_step = ["aguardando primeiro pullback curto de 1m"]
        elif phase == "PULLBACK 1M":
            next_step = ["aguardando retomada da microestrutura"]
        elif phase == "MICRO ARMADO":
            next_step = ["aguardando primeiro pullback de 1m sem perder a estrutura"]
        elif phase == "MICRO PULLBACK":
            next_step = ["aguardando reclaim com OI e taker confirmando"]
        elif phase == "ACUMULAÇÃO ARMADA":
            next_step = ["aguardando rompimento com OI persistente e LSR descendente"]
        elif phase == "ROMPIMENTO DETECTADO":
            next_step = ["aguardando sustentação ou reteste confirmado"]
        elif phase == "EXTREMO ARMADO":
            next_step = ["aguardando CHoCH, deterioração do OI 15m e virada do fluxo"]
        elif phase == "CHOCH DETECTADO":
            next_step = ["aguardando candle seguinte sustentar a nova estrutura"]
        elif phase == "TENDÊNCIA ARMADA":
            next_step = ["aguardando correção real sem perder EMA21 e OI"]
        elif phase == "CORREÇÃO DA TENDÊNCIA":
            next_step = ["aguardando retomada da estrutura com OI e taker"]
        if phase == "DUMP ARMADO":
            next_step = ["aguardando 15 min sem novo dump; depois OI+, LSR caindo e taker comprador"]
        elif phase == "BORDAS ARMADAS":
            next_step = ["aguardando rejeicao da borda, rompimento confirmado ou falso rompimento"]
        elif phase == "ROMPIMENTO DA BORDA":
            next_step = ["aguardando reteste sustentado ou retorno para dentro do range"]
        ranking.append({
            "symbol": row["symbol"], "strategy": row.get("strategy"), "decision": phase, "direction": row["direction"],
            "final_score": row["passed"], "risk_score": market["oi_change_15m_pct"],
            "confluence_count": round(market["volume_ratio"], 2),
            "block_reasons": next_step, "reasons": [],
            "regime": ", ".join(next_step) or "PRONTO PARA SINAL",
            "market": {
                "smart_money_bias": "", "smart_money_score": market["global_lsr"],
                "cvd_pct": (market["taker_buy_sell_ratio"] - 1.0) * 100.0,
                "whale_delta_pct": market["funding_rate_pct"],
                "absorption_score": market["spread_pct"], "absorption_side": "%",
                "short_squeeze_probability": abs(market["price_change_15m_pct"]),
                "long_squeeze_probability": abs(market["price_change_15m_pct"]),
                "last_price": market["price"],
            },
        })
    # The dashboard used to resend raw JSON/evidence for every historical row
    # every two seconds.  On LAN/mobile this grew beyond 2 MB and starved the
    # live position cards.  Keep complete data in SQLite and send only fields
    # that the UI actually renders.
    ui_closed = []
    for row in closed:
        compact = dict(row)
        compact.pop("trades_json", None)
        ui_closed.append(compact)
    ui_ledger = {"open": ledger["open"], "closed": ui_closed}
    reference_rows = []
    for row in (status.get("reference_ledger") or [])[:80]:
        compact = dict(row)
        compact.pop("evidence_json", None)
        reference_rows.append(compact)
    real_shadow = dict(status.get("real_shadow") or {})
    real_shadow["recent_trades"] = sorted([
        {key: value for key, value in row.items() if key not in {"signal_json", "execution_json"}}
        for row in (real_shadow.get("recent_trades") or [])[:120]
    ], key=lambda row: (
        0 if str(row.get("status") or "").upper() == "OPEN" else 1,
        -int(row.get("id") or 0),
    ))
    limited_shadow = dict(status.get("limited_shadow") or {})
    limited_shadow["recent_trades"] = sorted([
        {key: value for key, value in row.items() if key not in {"signal_json", "execution_json"}}
        for row in (limited_shadow.get("recent_trades") or [])[:120]
    ], key=lambda row: (
        0 if str(row.get("status") or "").upper() == "OPEN" else 1,
        -int(row.get("id") or 0),
    ))
    state = {
        "mode": dashboard_config()["mode"], "updated_at": now_label, "positions_updated_at": now_label,
        "binance_account": account, "binance_positions": _ui_positions(status),
        "binance_income_summary": account["income_summary"], "execution_ledger": ui_ledger,
        "reference_ledger": reference_rows,
        "reference_summary": status.get("reference_summary") or {},
        "strategy_performance": status.get("strategy_performance") or {},
        "real_shadow": real_shadow,
        "limited_shadow": limited_shadow,
        "live_positions": [], "scores": ranking, "errors": status["errors"],
        "heavy_symbols": status["universe"], "events": [
            {"id": row["id"], "kind": row["type"], "title": row["reason"], "message": row["reason"],
             "symbol": row["symbol"], "direction": row["direction"], "ts": row["timestamp_ms"]}
            for row in recent_events
        ],
        "scan_status": {"running": False, "idx": status["universe_count"], "total": status["universe_count"], "symbol": None},
        "clean_engine_metrics": {
            "impulses_armed": counts.get("CAMPAIGN_ARMED", 0) + counts.get("EXTREME_ARMED", 0) + counts.get("TREND_ARMED", 0),
            "pullbacks_observed": counts.get("BREAKOUT_DETECTED", 0) + counts.get("CHOCH_DETECTED", 0) + counts.get("TREND_PULLBACK", 0),
            "entries_ready": counts.get("SIGNAL", 0), "execution_opened": counts.get("EXECUTION_OPENED", 0),
            "execution_blocked": counts.get("SIGNAL_BLOCKED", 0) + counts.get("EXECUTION_FAILED", 0),
            "expired": counts.get("EXPIRED", 0), "invalidated": counts.get("INVALIDATED", 0),
            "currently_tracking": sum(len(items) for items in status["tracked_setups"].values()),
        },
    }
    summary = status["summary"]
    wins = sum(_number(row.get("pnl_net")) > 0 for row in closed)
    stats = {
        "balance": _number(account.get("totalWalletBalance")), "equity": _number(account.get("totalMarginBalance")),
        "pnl_total": _number(account.get("totalUnrealizedProfit")), "closed_trades": len(closed), "wins": wins,
        "losses": len(closed) - wins, "winrate": summary["win_rate_pct"], "profit_factor": summary["profit_factor"] or 0,
        "initial_balance": _number(account.get("totalWalletBalance")), "last_closed": ui_closed[:25],
    }
    return {"config": dashboard_config(), "state": state, "stats": stats}


@app.get("/health")
def health():
    status = service.status()
    return {"ok": True, "mode": status["mode"], "running": status["running"]}


@app.get("/api/status")
def api_status():
    return service.status()


@app.get("/api/signals")
def api_signals(limit: int = 100):
    return service.journal.recent_signals(max(1, min(limit, 500)))


@app.get("/api/events")
def api_events(limit: int = 100):
    return service.journal.recent_events(max(1, min(limit, 500)))


@app.get("/api/executions")
def api_executions(limit: int = 100):
    return service.journal.recent_executions(max(1, min(limit, 500)))


@app.get("/api/results")
def api_results(limit: int = 100):
    return service.journal.recent_execution_results(max(1, min(limit, 500)))


@app.get("/api/campaigns")
def api_campaigns(limit: int = 100):
    return service.journal.recent_campaign_actions(max(1, min(limit, 500)))


@app.get("/api/simulations")
def api_simulations():
    """Read-only laboratory ledger; these rows never represent Binance orders."""
    return service.simulation_lab.status()


@app.get("/api/state")
def api_legacy_state():
    return _legacy_state_payload()


@app.get("/api/live-positions")
def api_live_positions():
    """Small high-frequency feed; independent from the heavy dashboard payload."""
    with service.lock:
        execution_state = {
            "mode": service.execution_state.get("mode", config["mode"]),
            "positions": list(service.execution_state.get("positions") or []),
            "orders": list(service.execution_state.get("orders") or []),
            "algo_orders": list(service.execution_state.get("algo_orders") or []),
        }
        live_updated_ms = int(service.live_last_update_ms or 0)
    status = {"mode": config["mode"], "execution_state": execution_state}
    now_ms = int(time.time() * 1000)
    return {
        "ok": True,
        "updated_at": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
        "live_age_ms": max(0, now_ms - live_updated_ms) if live_updated_ms else None,
        "positions": _ui_positions(status),
    }


@app.get("/api/config")
def api_legacy_config():
    return dashboard_config()


@app.post("/api/config")
def api_legacy_config_write():
    return JSONResponse({"ok": False, "error": "Configuração antiga desativada; a engine usa somente o config.json limpo."}, status_code=409)


@app.get("/api/export/ledger/{ledger}")
def api_export_ledger(ledger: str):
    if ledger not in LEDGER_SPECS:
        raise HTTPException(status_code=404, detail="Ledger desconhecido")
    export_dir = ROOT / "tmp" / "ledger_exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        prefix=f"{ledger}_", suffix=".zip", dir=export_dir, delete=False,
    )
    output_path = Path(handle.name)
    handle.close()
    try:
        export_ledger_zip(ledger, config, ROOT, output_path)
    except FileNotFoundError as exc:
        output_path.unlink(missing_ok=True)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception:
        output_path.unlink(missing_ok=True)
        raise
    return FileResponse(
        output_path,
        media_type="application/zip",
        filename=export_filename(ledger),
        background=BackgroundTask(output_path.unlink, missing_ok=True),
    )


@app.get("/api/klines/{symbol}")
def api_klines(symbol: str, interval: str = Query("5m"), limit: int = Query(120)):
    allowed = {"1m", "3m", "5m", "15m", "30m", "1h"}
    selected = interval if interval in allowed else "5m"
    rows = service.chart_client.get("/fapi/v1/klines", {
        "symbol": symbol.upper(), "interval": selected, "limit": max(20, min(int(limit), 500)),
    })
    candles = [
        {"time": int(row[0]) // 1000, "open": _number(row[1]), "high": _number(row[2]),
         "low": _number(row[3]), "close": _number(row[4]), "volume": _number(row[5])}
        for row in rows
    ]
    source = "BINANCE TESTNET" if config["mode"] == "TESTNET" else "BINANCE REAL"
    return {"ok": True, "symbol": symbol.upper(), "source": source, "candles": candles}


@app.get("/api/derivatives-history/{symbol}")
def api_derivatives_history(symbol: str, period: str = Query("5m"), limit: int = Query(120)):
    selected = period if period in {"5m", "15m", "30m", "1h"} else "5m"
    size = max(20, min(int(limit), 500))
    name = symbol.upper()
    klines = service.chart_client.get("/fapi/v1/klines", {
        "symbol": name, "interval": selected, "limit": size,
    })
    oi = service.client.get("/futures/data/openInterestHist", {
        "symbol": name, "period": selected, "limit": size,
    })
    lsr = service.client.get("/futures/data/globalLongShortAccountRatio", {
        "symbol": name, "period": selected, "limit": size,
    })
    return {
        "ok": True, "symbol": name, "period": selected,
        "source": "BINANCE TESTNET + DERIVATIVOS PÚBLICOS" if config["mode"] == "TESTNET" else "BINANCE FUTURES",
        "price": [{"time": int(row[0]), "value": _number(row[4])} for row in klines],
        "oi": [{
            "time": int(row.get("timestamp") or 0),
            "value": _number(row.get("sumOpenInterest")),
            "notional": _number(row.get("sumOpenInterestValue")),
        } for row in oi],
        "lsr": [{
            "time": int(row.get("timestamp") or 0),
            "value": _number(row.get("longShortRatio")),
            "long": _number(row.get("longAccount")), "short": _number(row.get("shortAccount")),
        } for row in lsr],
    }


@app.get("/api/analysis/status")
def api_analysis_status():
    return {"ok": True, "dir": "data", "files": []}


@app.post("/api/execution/close-all")
def api_close_all(payload: dict):
    try:
        return service.close_all(str(payload.get("confirm") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/reset/all")
def api_reset_all(payload: dict):
    try:
        return service.reset_all(str(payload.get("confirm") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/minimal", response_class=HTMLResponse)
def minimal_dashboard():
    return HTMLResponse(
        """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>V10 OI Expansion</title><style>
body{margin:0;background:#091018;color:#dce7ef;font:14px system-ui}main{max-width:1180px;margin:auto;padding:24px}
h1{font-size:22px;margin:0 0 4px}.muted{color:#8295a5}.pill{display:inline-block;padding:5px 9px;border:1px solid #2a5565;border-radius:20px;color:#5ee1b2}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:20px 0}.card{background:#111d28;border:1px solid #203242;border-radius:10px;padding:14px}
.value{font-size:24px;margin-top:7px}table{width:100%;border-collapse:collapse;background:#111d28}th,td{text-align:left;padding:9px;border-bottom:1px solid #203242}th{color:#8295a5}
.long{color:#5ee1b2}.short{color:#ff8c8c}@media(max-width:760px){.grid{grid-template-columns:1fr 1fr}.scroll{overflow:auto}}
</style></head><body><main><span class="pill" id="mode">carregando</span><h1>V10 · OI Expansion</h1><div class="muted">Expansão confirmada de preço, OI e fluxo · Shadow, Testnet ou Real.</div>
<div class="grid"><div class="card">Abertos<div class="value" id="open">–</div></div><div class="card">Fechados<div class="value" id="closed">–</div></div><div class="card">Expectativa R<div class="value" id="expectancy">–</div></div><div class="card">Profit factor<div class="value" id="pf">–</div></div></div>
<div class="card"><b>Estado</b><div id="state" class="muted">carregando…</div></div><h2>Sinais</h2><div class="scroll"><table><thead><tr><th>Par</th><th>Direção</th><th>Status</th><th>Entrada</th><th>Stop</th><th>Alvo</th><th>R</th></tr></thead><tbody id="signals"></tbody></table></div>
<script>
const f=n=>Number(n||0).toFixed(3);async function refresh(){const [s,r]=await Promise.all([fetch('/api/status').then(x=>x.json()),fetch('/api/signals?limit=50').then(x=>x.json())]);
mode.textContent=s.mode+(s.execution_enabled?' · execução habilitada':' · execução desabilitada');open.textContent=s.summary.open;closed.textContent=s.summary.closed;expectancy.textContent=f(s.summary.expectancy_r);pf.textContent=s.summary.profit_factor===null?'–':f(s.summary.profit_factor);
state.textContent=`${s.running?'online':'parado'} · universo ${s.universe.length} pares · setups ${Object.keys(s.tracked_setups).length} · scan ${s.scan_duration_seconds}s · erros ${s.errors.length}`;
signals.innerHTML=r.map(x=>`<tr><td>${x.symbol}</td><td class="${x.direction==='LONG'?'long':'short'}">${x.direction}</td><td>${x.status}</td><td>${f(x.entry_price)}</td><td>${f(x.stop_price)}</td><td>${f(x.target_price)}</td><td>${x.result_r===null?'–':f(x.result_r)}</td></tr>`).join('')||'<tr><td colspan="7" class="muted">Nenhum sinal ainda. Isso é esperado: o gate é seletivo.</td></tr>'}refresh();setInterval(refresh,15000);
</script></main></body></html>"""
    )


@app.get("/", response_class=HTMLResponse)
def dashboard():
    html = (LEGACY_UI / "templates" / "dashboard.html").read_text(encoding="utf-8")
    values = {
        "{{ config.app_name }}": config["app_name"],
        "{{ state.mode }}": dashboard_config()["mode"],
        "{{ config.binance_min_request_gap_seconds }}": "0",
        "{{ config.chart_redraw_throttle_ms }}": "35",
        "{{ config.metrics_cache_seconds }}": "30",
        "{{ config.chart_drag_sensitivity }}": "2",
    }
    for source, value in values.items():
        html = html.replace(source, str(value))
    html = html.replace("CLEAN_ENTRY_V1", "ESTRATÉGIAS EM PARALELO")
    html = html.replace("CLEAN ENTRY V1", "ESTRATÉGIAS EM PARALELO")
    html = html.replace(
        '<p id="positionsUpdatedAt" class="muted"></p>\n        </div>\n    </div>\n    <div id="openPositionsCards" class="position-grid"></div>',
        '<p id="positionsUpdatedAt" class="muted"></p>\n        </div>\n'
        '        <div id="v10PositionViewControls" class="position-view-controls">'
        '<span class="muted">Visualizacao:</span>'
        '<button type="button" class="position-view-btn" data-open-view="cards" onclick="v10SetOpenView(\'cards\')">Cards</button>'
        '<button type="button" class="position-view-btn" data-open-view="line" onclick="v10SetOpenView(\'line\')">Linhas</button>'
        '</div>\n    </div>\n    <div id="openPositionsCards" class="position-grid"></div>',
    )
    html = html.replace("</head>", """
<style>
button[data-tab="config"],button[data-tab="info"],button[data-tab="analysis"],button[data-tab="backtest"],
#tab-config,#tab-info,#tab-analysis,#tab-backtest,.engine-funnel-grid>div:nth-child(n+7),
.top-actions a,#realModeToggleBtn,.top-actions button[onclick^="setTestnetValidationProfile"],#tab-closed .actions{display:none!important}
.position-main-line>span:nth-child(2),.institutional-strip,.card-extra-row{display:none!important}
.scanner-truth{margin:12px 0;padding:12px 14px;border:1px solid #30363d;border-radius:10px;background:#161b22;color:#8b949e}
.scanner-truth b{color:#f0f6fc}.scanner-truth .ok{color:#3fb950}
.derivatives-jaw{margin-top:14px;padding:14px;border:1px solid #30363d;border-radius:12px;background:#0d1117}
.derivatives-jaw.hidden{display:none}.derivatives-jaw-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;margin-bottom:8px}
.derivatives-jaw-head h3{margin:0;color:#f0f6fc}.derivatives-jaw-head p{margin:4px 0 0;color:#8b949e;font-size:12px}
.derivatives-jaw-legend{display:flex;gap:12px;flex-wrap:wrap;font-size:12px;color:#c9d1d9}.derivatives-jaw-legend span:before{content:'';display:inline-block;width:10px;height:3px;margin-right:5px;vertical-align:middle;background:var(--jaw-color)}
#derivativesJawCanvas{display:block;width:100%;height:250px;border-radius:8px;background:#0b1016}
.strategy-pill{display:inline-block;white-space:nowrap;padding:3px 7px;border-radius:999px;font-size:10px;font-weight:800;letter-spacing:.03em}
.strategy-pill.oi-expansion{color:#79c0ff;background:#0d2740;border:1px solid #1f6feb}
.strategy-pill.pre-explosion{color:#d2a8ff;background:#271640;border:1px solid #8957e5}
.strategy-pill.post-squeeze{color:#ffa657;background:#3a2410;border:1px solid #d29922}
.strategy-pill.momentum-early{color:#ff7b72;background:#3b1719;border:1px solid #f85149}
.strategy-pill.momentum-pullback{color:#56d4dd;background:#0d3035;border:1px solid #2ea9b0}
.strategy-group td{padding:10px 8px!important;background:#0d1117!important;border-top:2px solid #30363d!important;color:#c9d1d9;font-weight:700}
.strategy-group small{margin-left:8px;color:#8b949e;font-weight:400}
.strategy-row.pre-explosion td:first-child{border-left:3px solid #8957e5}.strategy-row.oi-expansion td:first-child{border-left:3px solid #1f6feb}
.strategy-row.post-squeeze td:first-child{border-left:3px solid #d29922}
.strategy-row.momentum-early td:first-child{border-left:3px solid #f85149}
.strategy-row.momentum-pullback td:first-child{border-left:3px solid #2ea9b0}
#tab-ranking table{min-width:1420px}#tab-ranking th,#tab-ranking td{white-space:nowrap}#tab-ranking td:nth-child(13){white-space:normal;min-width:220px}
.summary-cards>.summary-card:nth-child(11),.summary-cards>.summary-card:nth-child(12),.summary-cards>.summary-card:nth-child(13){display:none!important}
.shadow-toggle{border:1px solid #58a6ff;background:#0d2740;color:#79c0ff;border-radius:8px;padding:7px 10px;cursor:pointer;font-weight:700}
.ledger-export-actions{display:flex;gap:7px;align-items:center;flex-wrap:wrap;margin-top:10px;width:100%}.ledger-export-actions .ledger-label{color:#8b949e;font-size:12px;margin-right:2px}
.ledger-download{display:inline-block;border:1px solid #8957e5;background:#21143a;color:#d2a8ff;border-radius:8px;padding:7px 10px;text-decoration:none;font-size:12px;font-weight:800}.ledger-download:hover{border-color:#d2a8ff;color:#fff}
.ledger-reset{border:1px solid #f85149;background:#3b1719;color:#ff7b72;border-radius:8px;padding:7px 11px;font-size:12px;font-weight:900;cursor:pointer;margin-left:auto}.ledger-reset:hover{background:#5a1d20;color:#fff}.ledger-reset:disabled{opacity:.55;cursor:wait}
.real-shadow-card{border-color:#1f6feb!important}.real-shadow-card.hidden{display:none!important}
.real-shadow-panel{margin:10px 0 16px;padding:14px;border:1px solid #1f6feb;border-radius:10px;background:#0b1522;color:#c9d1d9}
.real-shadow-panel.hidden{display:none!important}.real-shadow-panel h3{margin:0 0 5px;color:#79c0ff}.real-shadow-panel p{margin:3px 0 10px;color:#9fb3c8}
.real-shadow-kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;margin:10px 0}.real-shadow-kpis div{padding:9px;border:1px solid #26384d;border-radius:8px;background:#0d1117}.real-shadow-kpis span{display:block;font-size:11px;color:#8b949e}.real-shadow-kpis strong{display:block;margin-top:4px;font-size:15px}
.real-shadow-table-wrap{overflow:auto}.real-shadow-table{width:100%;min-width:1050px;border-collapse:collapse;font-size:12px}.real-shadow-table th,.real-shadow-table td{padding:7px 8px;border-bottom:1px solid #26313d;text-align:left;white-space:nowrap}.real-shadow-table th{color:#8fb9df}.shadow-open{color:#3fb950;font-weight:800}.shadow-closed{color:#8b949e;font-weight:800}
.position-view-controls{display:flex;gap:7px;align-items:center;flex-wrap:wrap}
.position-view-btn,.jaw-toggle{border:1px solid #30363d;background:#161b22;color:#9da7b3;border-radius:9px;padding:7px 10px;cursor:pointer;font-size:12px;font-weight:850}
.position-view-btn:hover,.jaw-toggle:hover{border-color:#58a6ff;color:#e6edf3}.position-view-btn.active,.jaw-toggle.active{border-color:#238636;background:#12331f;color:#56d364}
#openPositionsCards.position-line-view{display:flex!important;flex-direction:column!important;gap:8px!important}
#openPositionsCards.position-line-view .position-card{display:grid!important;grid-template-columns:minmax(235px,.9fr) 90px minmax(190px,1fr) 125px minmax(420px,1.8fr);align-items:center;gap:10px;padding:9px 11px!important;min-height:0!important;border-radius:11px!important;transform:none!important}
#openPositionsCards.position-line-view .symbol-row{grid-column:1;min-width:0}
#openPositionsCards.position-line-view .position-main-line{grid-column:2;margin:0!important}
#openPositionsCards.position-line-view .setup-chip{grid-column:3;margin:0!important;min-width:0}
#openPositionsCards.position-line-view .order-age-pill{grid-column:4;margin:0!important;text-align:center;white-space:nowrap}
#openPositionsCards.position-line-view .metric-grid{grid-column:5;display:grid!important;grid-template-columns:repeat(4,minmax(92px,1fr))!important;gap:6px!important;margin:0!important}
#openPositionsCards.position-line-view .metric-grid .metric{display:none!important;padding:6px 8px!important;min-height:0!important}
#openPositionsCards.position-line-view .metric-grid .metric:nth-child(1),#openPositionsCards.position-line-view .metric-grid .metric:nth-child(2),#openPositionsCards.position-line-view .metric-grid .metric:nth-child(7),#openPositionsCards.position-line-view .metric-grid .metric:nth-child(8){display:block!important;grid-column:auto!important}
#openPositionsCards.position-line-view .metric-grid .metric span{font-size:10px!important}
#openPositionsCards.position-line-view .metric-grid .metric b{font-size:16px!important;white-space:nowrap}
#openPositionsCards.position-line-view .metric-grid .metric-pnl-live b{font-size:20px!important}
#openPositionsCards.position-line-view .entry-reason,#openPositionsCards.position-line-view .target-grid,#openPositionsCards.position-line-view .card-details,#openPositionsCards.position-line-view .progress-shell{display:none!important}
@media(max-width:1100px){#openPositionsCards.position-line-view .position-card{grid-template-columns:minmax(210px,1fr) 80px minmax(180px,1fr) 120px;}.position-line-view .metric-grid{grid-column:1/-1!important}}
@media(max-width:760px){.derivatives-jaw-head{display:block}.derivatives-jaw-legend{margin-top:8px}#derivativesJawCanvas{height:220px}.position-view-controls{width:100%}.position-view-btn{flex:1}#openPositionsCards.position-line-view .position-card{grid-template-columns:1fr auto!important;gap:7px!important}#openPositionsCards.position-line-view .symbol-row{grid-column:1!important}#openPositionsCards.position-line-view .position-main-line{grid-column:2!important}#openPositionsCards.position-line-view .setup-chip,#openPositionsCards.position-line-view .order-age-pill,#openPositionsCards.position-line-view .metric-grid{grid-column:1/-1!important;text-align:left}#openPositionsCards.position-line-view .metric-grid{grid-template-columns:1fr 1fr!important}}
</style></head>""")
    html = html.replace(
        '<div id="inlineChartBox"></div>',
        '<div id="inlineChartBox"></div><section id="derivativesJawPanel" class="derivatives-jaw hidden"><div class="derivatives-jaw-head"><div><h3>Boca do jacare: Preco x OI</h3><p id="derivativesJawStatus">Carregando OI e LSR de contas globais...</p></div><div class="derivatives-jaw-legend"><span style="--jaw-color:#facc15">Preco normalizado</span><span style="--jaw-color:#58a6ff">OI normalizado</span><span style="--jaw-color:#c084fc">LSR contas globais</span></div></div><canvas id="derivativesJawCanvas"></canvas></section>',
    )
    html = html.replace("</body>", """
<script>
const v10OriginalJawLoader=window.v10LoadDerivativesJaw;
window.v10JawVisible=()=>localStorage.getItem('v10_jaw_visible')!=='0';
window.v10LoadDerivativesJaw=async function(symbol){
  const panel=document.getElementById('derivativesJawPanel');
  if(!window.v10JawVisible()){if(panel)panel.classList.add('hidden');return;}
  return v10OriginalJawLoader?await v10OriginalJawLoader(symbol):undefined;
};
window.v10SetOpenView=function(mode){
  const selected=mode==='line'?'line':'cards';localStorage.setItem('v10_open_view',selected);
  const box=document.getElementById('openPositionsCards');if(box)box.classList.toggle('position-line-view',selected==='line');
  document.querySelectorAll('[data-open-view]').forEach(btn=>{const on=btn.dataset.openView===selected;btn.classList.toggle('active',on);btn.setAttribute('aria-pressed',String(on));});
};
window.v10ToggleJaw=function(){
  const showing=window.v10JawVisible();localStorage.setItem('v10_jaw_visible',showing?'0':'1');
  const panel=document.getElementById('derivativesJawPanel'),btn=document.getElementById('v10JawToggle');
  if(showing){if(panel)panel.classList.add('hidden');try{if(v10JawTimer){clearInterval(v10JawTimer);v10JawTimer=null;}}catch(e){}}
  else if(activeChartPosition){window.v10LoadDerivativesJaw(activeChartPosition.symbol);}
  if(btn){btn.textContent=showing?'Mostrar OI / LSR':'Ocultar OI / LSR';btn.classList.toggle('active',!showing);}
};
window.v10ResetAll=async function(){
  const phrase='ZERAR_TUDO_TESTNET_SHADOW';
  const typed=prompt('ATENCAO: encerra posicoes e ordens TESTNET e zera Testnet, Shadow Real, Shadow individual e simulacoes. Um backup automatico sera criado.\\n\\nDigite exatamente: '+phrase);
  if(typed===null)return;
  if(typed!==phrase){alert('Confirmacao incorreta. Nada foi alterado.');return;}
  if(!confirm('Ultima confirmacao: executar ZERAR TUDO agora?'))return;
  const button=document.getElementById('v10ResetAll');if(button){button.disabled=true;button.textContent='ZERANDO...';}
  try{
    const response=await fetch('/api/reset/all',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirm:phrase})});
    const data=await response.json();
    if(!response.ok)throw new Error(data.detail||'Falha ao zerar');
    alert('Reset concluido. Backup: '+data.backup_dir+'\\nNova amostra: '+data.sample_started_at);
    location.reload();
  }catch(error){alert('RESET NAO CONCLUIDO: '+error.message);if(button){button.disabled=false;button.textContent='ZERAR TUDO';}}
};
document.addEventListener('DOMContentLoaded',()=>{
  const liveTitle=document.querySelector('#tab-live .panel-title');
  if(liveTitle&&!document.getElementById('v10PositionViewControls')){
    const controls=document.createElement('div');controls.className='position-view-controls';
    controls.id='v10PositionViewControls';
    controls.innerHTML=`<button type="button" class="position-view-btn" data-open-view="cards" onclick="v10SetOpenView('cards')">Cards</button><button type="button" class="position-view-btn" data-open-view="line" onclick="v10SetOpenView('line')">Linhas</button>`;
    liveTitle.appendChild(controls);v10SetOpenView(localStorage.getItem('v10_open_view')||'cards');
  }
  const chartActions=document.querySelector('#inlineChartPanel .chart-actions');
  if(chartActions){
    const jaw=document.createElement('button');jaw.type='button';jaw.id='v10JawToggle';jaw.className='jaw-toggle';jaw.onclick=v10ToggleJaw;
    jaw.textContent=v10JawVisible()?'Ocultar OI / LSR':'Mostrar OI / LSR';jaw.classList.toggle('active',v10JawVisible());
    chartActions.insertBefore(jaw,chartActions.lastElementChild);
  }
  const section=document.getElementById('tab-ranking');
  if(section){
    const title=section.querySelector('h2'); if(title) title.textContent='Candidatos · Estratégias em paralelo';
    const header=section.querySelector('thead tr');
    if(header) header.innerHTML='<th>Estratégia</th><th>Par</th><th>Estado</th><th>Direção</th><th>Gates OK</th><th>OI 15m %</th><th>Volume x</th><th>LSR contas</th><th>Taker Δ %</th><th>Funding %</th><th>Spread %</th><th>Impulso %</th><th>Próxima etapa</th><th>Preço</th>';
  }
  const funnel=document.querySelector('.engine-funnel');
  if(funnel){const box=document.createElement('div');box.id='scannerTruth';box.className='scanner-truth';funnel.insertAdjacentElement('afterend',box);}
  if(funnel) funnel.insertAdjacentHTML('afterend',`<section id="v10RealShadowPanel" class="real-shadow-panel hidden">
    <h3>Simula&ccedil;&atilde;o Binance Real (Shadow)</h3>
    <p><b>N&atilde;o envia ordens.</b> Usa os mesmos sinais da engine, mas valida entrada, spread, profundidade, pre&ccedil;o e sa&iacute;da com dados da Binance de produ&ccedil;&atilde;o.</p>
    <div class="real-shadow-kpis">
      <div><span>Estado</span><strong id="v10ShadowEnabled">-</strong></div>
      <div><span>Abertas</span><strong id="v10ShadowOpen">0</strong></div>
      <div><span>Fechadas</span><strong id="v10ShadowClosed">0</strong></div>
      <div><span>PNL fechado</span><strong id="v10ShadowPnl">0.00 USDT</strong></div>
      <div><span>PNL aberto agora</span><strong id="v10ShadowOpenPnl">0.00 USDT</strong></div>
      <div><span>PNL total agora</span><strong id="v10ShadowTotalPnl">0.00 USDT</strong></div>
      <div><span>WR / PF</span><strong id="v10ShadowQuality">0% / -</strong></div>
    </div>
    <div class="real-shadow-table-wrap"><table class="real-shadow-table"><thead><tr><th>Estado</th><th>Par</th><th>Dire&ccedil;&atilde;o</th><th>Estrat&eacute;gia</th><th>Entrada Real</th><th>Atual / Sa&iacute;da</th><th>Stop atual</th><th>Alvo l&oacute;gico</th><th>PNL</th><th>ROE margem</th><th>Tempo aberto</th><th>Motivo</th></tr></thead><tbody id="v10ShadowTrades"><tr><td colspan="12">Aguardando dados da simula&ccedil;&atilde;o Real...</td></tr></tbody></table></div>
  </section>`);
  const shadowPanel=document.getElementById('v10RealShadowPanel');
  if(shadowPanel) shadowPanel.insertAdjacentHTML('afterend',`<section id="v10LimitedShadowPanel" class="real-shadow-panel hidden">
    <h3>Shadow Real Limitada &middot; m&aacute;ximo 2 posi&ccedil;&otilde;es</h3>
    <p><b>N&atilde;o envia ordens.</b> Usa exatamente a mesma execu&ccedil;&atilde;o, geometria, margem, alavancagem e runner da Shadow Real, limitando apenas a duas entradas simult&acirc;neas.</p>
    <div class="real-shadow-kpis">
      <div><span>Estado</span><strong id="v10LimitedEnabled">ATIVA</strong></div><div><span>Abertas</span><strong id="v10LimitedOpen">0</strong></div><div><span>Fechadas</span><strong id="v10LimitedClosed">0</strong></div>
      <div><span>PNL fechado</span><strong id="v10LimitedClosedPnl">0.00 USDT</strong></div><div><span>PNL aberto agora</span><strong id="v10LimitedOpenPnl">0.00 USDT</strong></div><div><span>PNL total agora</span><strong id="v10LimitedTotalPnl">0.00 USDT</strong></div><div><span>WR / PF</span><strong id="v10LimitedQuality">0% / -</strong></div>
    </div>
    <div class="real-shadow-table-wrap"><table class="real-shadow-table"><thead><tr><th>Estado</th><th>Par</th><th>Dire&ccedil;&atilde;o</th><th>Estrat&eacute;gia</th><th>Entrada</th><th>Atual / Sa&iacute;da</th><th>Stop atual</th><th>Alvo l&oacute;gico</th><th>PNL</th><th>ROE margem</th><th>Tempo aberto</th><th>Motivo</th></tr></thead><tbody id="v10LimitedTrades"><tr><td colspan="12">Aguardando novos sinais...</td></tr></tbody></table></div>
  </section>`);
  const funnelTitle=document.querySelector('.engine-funnel-title strong');
  if(funnelTitle) funnelTitle.textContent='Resultados por estratégia · Binance Testnet';
  const strategyNote=document.getElementById('watcherFunnelNote');
  if(strategyNote) strategyNote.id='v10StrategyNote';
  const strategyGrid=document.querySelector('.engine-funnel-grid');
  if(strategyGrid) strategyGrid.innerHTML=`
    <div><span>Scalp de exaustão</span><strong id="v10ScalpStats">0 trades</strong></div>
    <div><span>Scalp de continuação</span><strong id="v10ScalpContinuationStats">0 trades</strong></div>
    <div><span>Dump reversão LONG</span><strong id="v10DumpReversalStats">0 trades</strong></div>
    <div><span>Dump continuação SHORT</span><strong id="v10DumpContinuationStats">0 trades</strong></div>
    <div><span>Bordas</span><strong id="v10BorderStats">0 trades</strong></div>
    <div><span>Shadow limitada &middot; m&aacute;ximo 2</span><strong id="v10LimitedStats">0 trades</strong></div>
    <div><span>Total Binance Testnet</span><strong id="v10TestnetTotalStats">0 trades</strong></div>
    <div id="v10RealShadowCard" class="real-shadow-card hidden"><span>REAL SHADOW · sem ordem</span><strong id="v10RealShadowStats">0 trades</strong></div>`;
  const funnelHead=document.querySelector('.engine-funnel-title');
  if(funnelHead){
    const toggle=document.createElement('button');toggle.id='v10ShadowToggle';toggle.className='shadow-toggle';toggle.type='button';toggle.textContent='Mostrar simulação Real';
    toggle.onclick=()=>{const card=document.getElementById('v10RealShadowCard');const panel=document.getElementById('v10RealShadowPanel');const showing=panel?.classList.toggle('hidden')===false;if(card)card.classList.toggle('hidden',!showing);toggle.textContent=showing?'Ocultar simulação Real':'Mostrar simulação Real';};
    funnelHead.appendChild(toggle);
    const levToggle=document.createElement('button');levToggle.id='v10LimitedToggle';levToggle.className='shadow-toggle';levToggle.type='button';levToggle.textContent='Mostrar Shadow limitada';
    levToggle.onclick=()=>{const panel=document.getElementById('v10LimitedShadowPanel');const showing=panel?.classList.toggle('hidden')===false;levToggle.textContent=showing?'Ocultar Shadow limitada':'Mostrar Shadow limitada';};
    funnelHead.appendChild(levToggle);
    const exports=document.createElement('div');exports.id='v10LedgerExports';exports.className='ledger-export-actions';
    exports.innerHTML=`<span class="ledger-label">Ledgers para enviar &agrave; an&aacute;lise:</span><a class="ledger-download" href="/api/export/ledger/testnet">Baixar Testnet</a><a class="ledger-download" href="/api/export/ledger/shadow">Baixar Shadow Real</a><a class="ledger-download" href="/api/export/ledger/limited">Baixar Shadow individual</a><a class="ledger-download" href="/api/export/ledger/simulations">Baixar corre&ccedil;&atilde;o staged</a><button type="button" id="v10ResetAll" class="ledger-reset" onclick="v10ResetAll()">ZERAR TUDO</button>`;
    funnelHead.appendChild(exports);
  }
  let lastStrategyStatus=null;
  const metric=(x)=>{
    x=x||{};const pf=x.profit_factor==null?'-':Number(x.profit_factor).toFixed(2);
    return `${Number(x.closed||0)} fech. / ${Number(x.open||0)} ab. · WR ${Number(x.win_rate_pct||0).toFixed(1)}% · PF ${pf} · ${Number(x.net_pnl||0).toFixed(2)} USDT`;
  };
  const merge=(items)=>{
    const rows=items.filter(Boolean),closed=rows.reduce((n,x)=>n+Number(x.closed||0),0),open=rows.reduce((n,x)=>n+Number(x.open||0),0);
    const net=rows.reduce((n,x)=>n+Number(x.net_pnl||0),0),wins=rows.reduce((n,x)=>n+Number(x.wins||0),0);
    const gp=rows.reduce((n,x)=>n+Number(x.gross_profit||0),0),gl=rows.reduce((n,x)=>n+Number(x.gross_loss||0),0);
    return {closed,open,net_pnl:net,win_rate_pct:closed?wins/closed*100:0,profit_factor:gl?gp/gl:(gp?Infinity:null)};
  };
  const shadowStrategyName=(name)=>({
    VOLATILITY_EXHAUSTION_FADE_SCALP_V1:'Scalp exaustao',VOLATILITY_EXHAUSTION_CONTINUATION_SCALP_V1:'Scalp continuacao',DUMP_REVERSAL_LONG:'Dump reversao',DUMP_CONTINUATION_SHORT:'Dump continuacao'
  }[String(name||'')]||String(name||'-').replaceAll('_',' '));
  const shadowDuration=(t)=>{
    const start=Number(t.opened_at_ms||0),end=Number(t.closed_at_ms||(Date.now()));if(!start)return '-';
    let total=Math.max(0,Math.floor((end-start)/1000)),hours=Math.floor(total/3600);total%=3600;const minutes=Math.floor(total/60),seconds=total%60;
    return hours?`${hours}h ${String(minutes).padStart(2,'0')}m ${String(seconds).padStart(2,'0')}s`:`${minutes}m ${String(seconds).padStart(2,'0')}s`;
  };
  function paintRealShadow(shadow){
    shadow=shadow||{};const sum=shadow.summary||{},trades=Array.isArray(shadow.recent_trades)?shadow.recent_trades:[];
    const pf=sum.profit_factor==null?'-':Number(sum.profit_factor).toFixed(2),pnl=Number(sum.closed_pnl??sum.net_pnl??0),openPnl=Number(sum.open_unrealized_pnl||0),totalPnl=Number(sum.total_pnl??(pnl+openPnl));
    const put=(id,value,klass='')=>{const el=document.getElementById(id);if(el){el.textContent=value;el.className=klass;}};
    put('v10ShadowEnabled',shadow.enabled?'ATIVA · SEM ORDENS':'DESATIVADA',shadow.enabled?'positive':'negative');
    put('v10ShadowOpen',String(Number(sum.open||0)));put('v10ShadowClosed',String(Number(sum.closed||0)));
    put('v10ShadowPnl',`${pnl>=0?'+':''}${pnl.toFixed(2)} USDT`,pnl>=0?'positive':'negative');
    put('v10ShadowOpenPnl',`${openPnl>=0?'+':''}${openPnl.toFixed(2)} USDT`,openPnl>=0?'positive':'negative');
    put('v10ShadowTotalPnl',`${totalPnl>=0?'+':''}${totalPnl.toFixed(2)} USDT`,totalPnl>=0?'positive':'negative');
    put('v10ShadowQuality',`${Number(sum.win_rate_pct||0).toFixed(1)}% / ${pf}`);
    const body=document.getElementById('v10ShadowTrades');if(!body)return;
    if(!trades.length){body.innerHTML='<tr><td colspan="12">Nenhuma opera&ccedil;&atilde;o Real Shadow nesta amostra.</td></tr>';return;}
    body.innerHTML=trades.slice(0,50).map(t=>{
      const open=String(t.status||'').toUpperCase()==='OPEN',pnlValue=Number(open?t.unrealized_pnl:t.net_pnl),roe=Number(open?t.unrealized_margin_pct:(Number(t.net_pnl||0)/Number(t.margin_usdt||1)*100));
      const validPnl=Number.isFinite(pnlValue),validRoe=Number.isFinite(roe);
      const pnlText=validPnl?`${pnlValue>=0?'+':''}${pnlValue.toFixed(2)} USDT`:'-';
      const roeText=validRoe?`${roe>=0?'+':''}${roe.toFixed(2)}%`:'-';
      const result=!open?(pnlValue>0?'WIN':pnlValue<0?'LOSS':'ZERO'):'EM ANDAMENTO';
      return `<tr><td class="${open?'shadow-open':'shadow-closed'}">${open?'ABERTA':'FECHADA'}</td><td><b>${t.symbol||'-'}</b></td><td class="${String(t.direction).toUpperCase()==='LONG'?'positive':'negative'}">${t.direction||'-'}</td><td>${shadowStrategyName(t.strategy)}</td><td>${fmt(t.entry_price,8)}</td><td>${fmt(open?t.current_price:t.exit_price,8)}</td><td>${fmt(t.current_stop_price,8)}</td><td>${fmt(t.target_price,8)}</td><td class="${validPnl&&pnlValue>=0?'positive':'negative'}">${pnlText}</td><td class="${validRoe&&roe>=0?'positive':'negative'}">${roeText}</td><td>${shadowDuration(t)}</td><td>${result}${t.exit_reason?' · '+t.exit_reason:''}</td></tr>`;
    }).join('');
  }
  function paintLev20Simulation(sim,latest){
    sim=sim||{};latest=latest||{};const prefix='LEV20_HALF_GEOMETRY::';
    const trades=(Array.isArray(sim.recent_trades)?sim.recent_trades:[]).filter(t=>String(t.strategy||'').startsWith(prefix)).sort((a,b)=>{const ao=String(a.status).toUpperCase()==='OPEN',bo=String(b.status).toUpperCase()==='OPEN';return ao!==bo?(ao?-1:1):Number(b.id||0)-Number(a.id||0);});
    const summaries=Object.entries(sim.summary||{}).filter(([k])=>k.startsWith(prefix)).map(([,v])=>v);
    const closed=summaries.reduce((n,x)=>n+Number(x.closed||0),0),opened=trades.filter(t=>String(t.status).toUpperCase()==='OPEN').length,wins=summaries.reduce((n,x)=>n+Number(x.wins||0),0);
    const closedPnl=summaries.reduce((n,x)=>n+Number(x.net_pnl_usdt||0),0),gp=summaries.reduce((n,x)=>n+Number(x.gross_profit||0),0),gl=summaries.reduce((n,x)=>n+Number(x.gross_loss||0),0),pf=gl?gp/gl:(gp?Infinity:null),wr=closed?wins/closed*100:0;
    const liveMetrics=t=>{const e=t.evidence||{},entry=Number(t.entry_price||0),current=Number(t.current_price||(latest[t.symbol]||{}).price||entry);if(t.estimated_open_pnl_usdt!=null)return {current,pnl:Number(t.estimated_open_pnl_usdt),roe:Number(t.estimated_open_roe_pct||0)};const long=String(t.direction).toUpperCase()==='LONG',exit=current*(long?0.9998:1.0002),raw=entry?((exit-entry)/entry*100)*(long?1:-1):0,netPct=raw-0.10,notional=Number(e.notional_usdt||1000),margin=Number(e.margin_usdt||50),pnl=netPct/100*notional;return {current,pnl,roe:margin?pnl/margin*100:0};};
    const openPnl=trades.filter(t=>String(t.status).toUpperCase()==='OPEN').reduce((n,t)=>n+liveMetrics(t).pnl,0),total=closedPnl+openPnl;
    const put=(id,value,klass='')=>{const el=document.getElementById(id);if(el){el.textContent=value;el.className=klass;}};
    put('v10Lev20Enabled',sim.enabled?'ATIVA · SEM ORDENS':'DESATIVADA',sim.enabled?'positive':'negative');put('v10Lev20Open',String(opened));put('v10Lev20Closed',String(closed));
    put('v10Lev20ClosedPnl',`${closedPnl>=0?'+':''}${closedPnl.toFixed(2)} USDT`,closedPnl>=0?'positive':'negative');put('v10Lev20OpenPnl',`${openPnl>=0?'+':''}${openPnl.toFixed(2)} USDT`,openPnl>=0?'positive':'negative');put('v10Lev20TotalPnl',`${total>=0?'+':''}${total.toFixed(2)} USDT`,total>=0?'positive':'negative');put('v10Lev20Quality',`${wr.toFixed(1)}% / ${pf==null?'-':Number(pf).toFixed(2)}`);
    const body=document.getElementById('v10Lev20Trades');if(!body)return;if(!trades.length){body.innerHTML='<tr><td colspan="12">Nenhuma opera&ccedil;&atilde;o 20x nesta amostra.</td></tr>';return;}
    body.innerHTML=trades.slice(0,80).map(t=>{const open=String(t.status).toUpperCase()==='OPEN',e=t.evidence||{},live=open?liveMetrics(t):null,pnl=open?live.pnl:Number(t.estimated_net_pnl_usdt||0),roe=open?live.roe:Number(t.estimated_roe_pct||0),price=open?live.current:Number(t.exit_price||0),setup=String(e.source_setup||t.strategy||'-').replace(prefix,'');return `<tr><td class="${open?'shadow-open':'shadow-closed'}">${open?'ABERTA':'FECHADA'}</td><td><b>${t.symbol||'-'}</b></td><td class="${String(t.direction).toUpperCase()==='LONG'?'positive':'negative'}">${t.direction||'-'}</td><td>${shadowStrategyName(setup)}</td><td>${fmt(t.entry_price,8)}</td><td>${fmt(price,8)}</td><td>${fmt(t.stop_price,8)}</td><td>${fmt(e.native_target_price,8)}</td><td class="${pnl>=0?'positive':'negative'}">${pnl>=0?'+':''}${pnl.toFixed(2)} USDT</td><td class="${roe>=0?'positive':'negative'}">${roe>=0?'+':''}${roe.toFixed(2)}%</td><td>${shadowDuration(t)}</td><td>${open?'EM ANDAMENTO':(pnl>0?'WIN':pnl<0?'LOSS':'ZERO')}${t.exit_reason?' · '+t.exit_reason:''}</td></tr>`;}).join('');
  }
  function paintLimitedShadow(shadow){
    shadow=shadow||{};const sum=shadow.summary||{},trades=Array.isArray(shadow.recent_trades)?shadow.recent_trades:[];
    const closedPnl=Number(sum.closed_pnl??sum.net_pnl??0),openPnl=Number(sum.open_unrealized_pnl||0),total=Number(sum.total_pnl??(closedPnl+openPnl));
    const pf=sum.profit_factor==null?'-':Number(sum.profit_factor).toFixed(2),put=(id,value,klass='')=>{const el=document.getElementById(id);if(el){el.textContent=value;el.className=klass;}};
    put('v10LimitedEnabled',shadow.enabled?'ATIVA · LIMITE 2':'DESATIVADA',shadow.enabled?'positive':'negative');put('v10LimitedOpen',String(Number(sum.open||0)));put('v10LimitedClosed',String(Number(sum.closed||0)));
    put('v10LimitedClosedPnl',`${closedPnl>=0?'+':''}${closedPnl.toFixed(2)} USDT`,closedPnl>=0?'positive':'negative');put('v10LimitedOpenPnl',`${openPnl>=0?'+':''}${openPnl.toFixed(2)} USDT`,openPnl>=0?'positive':'negative');put('v10LimitedTotalPnl',`${total>=0?'+':''}${total.toFixed(2)} USDT`,total>=0?'positive':'negative');put('v10LimitedQuality',`${Number(sum.win_rate_pct||0).toFixed(1)}% / ${pf}`);
    const body=document.getElementById('v10LimitedTrades');if(!body)return;if(!trades.length){body.innerHTML='<tr><td colspan="12">Nenhuma opera&ccedil;&atilde;o na Shadow limitada.</td></tr>';return;}
    body.innerHTML=trades.slice(0,80).map(t=>{const open=String(t.status||'').toUpperCase()==='OPEN',pnl=Number(open?t.unrealized_pnl:t.net_pnl),roe=Number(open?t.unrealized_margin_pct:(Number(t.net_pnl||0)/Number(t.margin_usdt||1)*100)),price=Number(open?t.current_price:t.exit_price),result=open?'EM ANDAMENTO':(pnl>0?'WIN':pnl<0?'LOSS':'ZERO');return `<tr><td class="${open?'shadow-open':'shadow-closed'}">${open?'ABERTA':'FECHADA'}</td><td><b>${t.symbol||'-'}</b></td><td class="${String(t.direction).toUpperCase()==='LONG'?'positive':'negative'}">${t.direction||'-'}</td><td>${shadowStrategyName(t.strategy)}</td><td>${fmt(t.entry_price,8)}</td><td>${fmt(price,8)}</td><td>${fmt(t.current_stop_price,8)}</td><td>${fmt(t.target_price,8)}</td><td class="${pnl>=0?'positive':'negative'}">${pnl>=0?'+':''}${pnl.toFixed(2)} USDT</td><td class="${roe>=0?'positive':'negative'}">${roe>=0?'+':''}${roe.toFixed(2)}%</td><td>${shadowDuration(t)}</td><td>${result}${t.exit_reason?' · '+t.exit_reason:''}</td></tr>`;}).join('');
  }
  function paintStrategyCards(){
    const s=lastStrategyStatus;if(!s)return;const p=s.strategy_performance||{};
    const border=merge(Object.entries(p).filter(([k])=>k.includes('BORDER_')||k.includes('RANGE_EDGE_')||k.includes('FAILED_BREAKOUT_')).map(([,v])=>v));
    const ids=['v10ScalpStats','v10ScalpContinuationStats','v10DumpReversalStats','v10DumpContinuationStats','v10BorderStats','v10LimitedStats','v10TestnetTotalStats','v10RealShadowStats'];
    const values=[p.VOLATILITY_EXHAUSTION_FADE_SCALP_V1,p.VOLATILITY_EXHAUSTION_CONTINUATION_SCALP_V1,p.DUMP_REVERSAL_LONG,p.DUMP_CONTINUATION_SHORT,border,s.limited_shadow?.summary,s.summary,s.real_shadow?.summary];
    ids.forEach((id,i)=>{const el=document.getElementById(id);if(el)el.textContent=metric(values[i]);});
    paintRealShadow(s.real_shadow);
    paintLimitedShadow(s.limited_shadow);
    const note=document.getElementById('v10StrategyNote');if(note)note.textContent='Testnet visível · simulação Real disponível sob demanda';
  }
  async function refreshScannerTruth(){
    try{
      const s=await fetch('/api/status?ts='+Date.now()).then(r=>r.json());
      lastStrategyStatus=s;paintStrategyCards();
      const last=s.last_scan_ms?new Date(s.last_scan_ms).toLocaleTimeString('pt-BR'):'aguardando';
      const liveAge=s.live_age_ms==null?'aguardando':(Number(s.live_age_ms)/1000).toFixed(1)+'s';
      const box=document.getElementById('scannerTruth');
      const tracked=Object.values(s.tracked_setups||{}).reduce((n,items)=>n+Object.keys(items||{}).length,0);
      const modes=Object.entries(s.strategy_modes||{});
      const executing=modes.filter(([,mode])=>mode==='EXECUTE').map(([name])=>name).join(', ')||'nenhuma';
      const observing=modes.filter(([,mode])=>mode==='OBSERVE').map(([name])=>name).join(', ')||'nenhuma';
      if(box) box.innerHTML=`<b>Scanner:</b> <span class="ok">${s.universe_count} futuros varridos</span> · ${s.liquid_universe_count} com liquidez · ${s.prefiltered_count} candidatos pesados · ${tracked} setups acompanhados · ciclo ${Number(s.scan_duration_seconds||0).toFixed(1)}s · último ${last} · erros ${s.errors.length}`;
      if(box) box.innerHTML=`<b>Binance live:</b> <span class="ok">idade ${liveAge}</span> | `+box.innerHTML;
      if(box) box.innerHTML+=`<br><b>Executando:</b> <span class="ok">${executing}</span> · <b>Somente observando:</b> ${observing}`;
      const actions=(s.campaign_actions||[]).slice(0,5).map(a=>`${a.symbol} ${a.type.replace('CAMPAIGN_','')} (${a.reason})`).join(' · ');
      if(box&&s.campaign_enabled) box.innerHTML+=`<br><b>Gestão ativa:</b> reversão exige confirmação ≥1 · score 0 arma continuação após micro rompimento · máximo 3 teses na mesma direção · invalidação sem desenvolvimento · runner acima dos custos · sem parcial${actions?' · <b>Últimas:</b> '+actions:''}`;
    }catch(e){}
  }
  refreshScannerTruth();setInterval(refreshScannerTruth,15000);
});
</script></body>""")
    return HTMLResponse(html)


if __name__ == "__main__":
    uvicorn.run(app, host=config["host"], port=int(config["port"]))
