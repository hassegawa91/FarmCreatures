from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.json"


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    config = json.loads(path.read_text(encoding="utf-8"))
    mode = str(config.get("mode") or "").upper()
    if mode not in {"SHADOW", "TESTNET", "REAL"}:
        raise RuntimeError("mode deve ser SHADOW, TESTNET ou REAL")
    strategy = config.get("strategy") or {}
    if strategy.get("name") not in {"DUMP_EXHAUSTION_RECLAIM_V1", "DUMP_REGIME_V2"}:
        raise RuntimeError("Apenas as engines causais DUMP_EXHAUSTION_RECLAIM_V1 ou DUMP_REGIME_V2 sao permitidas")
    valid_strategy_modes = {"EXECUTE", "OBSERVE"}
    modes = config.get("strategy_modes") or {}
    if any(str(mode).upper() not in valid_strategy_modes for mode in modes.values()):
        raise RuntimeError("strategy_modes deve usar apenas EXECUTE ou OBSERVE")
    campaign = config.get("campaign") or {}
    if campaign.get("enabled"):
        valid_roles = {"PROBE", "CONFIRM", "REVERSAL", "OBSERVE"}
        roles = campaign.get("strategy_roles") or {}
        if any(str(role).upper() not in valid_roles for role in roles.values()):
            raise RuntimeError("campaign.strategy_roles deve usar PROBE, CONFIRM, REVERSAL ou OBSERVE")
        for role in ("probe", "confirm", "reversal"):
            risk = float(campaign.get(f"{role}_account_risk_pct", 0.0))
            minimum = float(campaign.get(f"{role}_min_stop_pct", 0.0))
            maximum = float(campaign.get(f"{role}_max_stop_pct", 0.0))
            if risk <= 0 or minimum <= 0 or maximum < minimum:
                raise RuntimeError(f"configuracao de risco invalida para campaign.{role}")
    return config
