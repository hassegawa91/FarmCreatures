from __future__ import annotations

import json
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SENSITIVE_KEY_PARTS = (
    "api_key", "api_secret", "secret", "token", "password", "credential",
    "private_key", "passphrase",
)

LEDGER_SPECS: dict[str, tuple[str, tuple[str, ...]]] = {
    "testnet": (
        "database_path",
        ("signals", "executions", "execution_results", "events", "feature_observations"),
    ),
    "shadow": (
        "real_shadow.database_path",
        ("real_shadow_trades", "real_shadow_events"),
    ),
    "limited": (
        "limited_shadow.database_path",
        ("real_shadow_trades", "real_shadow_events"),
    ),
    "simulations": (
        "simulation_lab.database_path",
        ("simulation_trades", "simulation_events"),
    ),
}


def sanitize(value: Any, key: str = "") -> Any:
    normalized = key.lower().replace("-", "_")
    if any(part in normalized for part in SENSITIVE_KEY_PARTS):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): sanitize(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    return value


def _config_value(config: dict[str, Any], dotted_key: str) -> Any:
    value: Any = config
    for part in dotted_key.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _decode_json_columns(row: dict[str, Any]) -> dict[str, Any]:
    decoded: dict[str, Any] = {}
    for key, value in row.items():
        if key.endswith("_json") and isinstance(value, str):
            try:
                decoded[key[:-5]] = sanitize(json.loads(value))
            except json.JSONDecodeError:
                decoded[key] = value
        else:
            decoded[key] = sanitize(value, key)
    return decoded


def export_ledger_zip(
    ledger: str, config: dict[str, Any], root: Path, output_path: Path,
) -> dict[str, Any]:
    if ledger not in LEDGER_SPECS:
        raise ValueError(f"Ledger desconhecido: {ledger}")
    path_key, requested_tables = LEDGER_SPECS[ledger]
    configured_path = _config_value(config, path_key)
    if not configured_path:
        raise FileNotFoundError(f"Caminho não configurado para o ledger {ledger}")
    database_path = (root / str(configured_path)).resolve()
    if not database_path.is_file():
        raise FileNotFoundError(f"Ledger não encontrado: {database_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "format": "cryptoshadow-ledger-export-v1",
        "ledger": ledger,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "sample_started_at": config.get("sample_started_at"),
        "management_revision": (config.get("strategy") or {}).get("management_revision"),
        "database_filename": database_path.name,
        "tables": {},
        "security": "Segredos, tokens, senhas, credenciais e chaves são removidos.",
    }

    db = sqlite3.connect(database_path.as_uri() + "?mode=ro", uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        existing = {
            str(row[0]) for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for table in requested_tables:
                if table not in existing:
                    manifest["tables"][table] = {"rows": 0, "missing": True}
                    continue
                columns = [str(row[1]) for row in db.execute(f'PRAGMA table_info("{table}")')]
                order = " ORDER BY id" if "id" in columns else ""
                count = 0
                with archive.open(f"tables/{table}.jsonl", "w") as target:
                    for raw in db.execute(f'SELECT * FROM "{table}"{order}'):
                        row = _decode_json_columns(dict(raw))
                        target.write((json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
                        count += 1
                manifest["tables"][table] = {"rows": count, "columns": columns}

            archive.writestr(
                "config.sanitized.json",
                json.dumps(sanitize(config), ensure_ascii=False, indent=2),
            )
            archive.writestr(
                "README.txt",
                "CryptoShadow V10 - pacote de ledger para análise\n"
                "Os arquivos tables/*.jsonl possuem um objeto JSON completo por linha.\n"
                "Envie este ZIP ao ChatGPT e peça comparação por setup, direção, regime, MFE/MAE e saída.\n"
                "Nenhuma chave ou credencial deve estar presente neste pacote.\n",
            )
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    finally:
        db.close()
    return manifest


def export_filename(ledger: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"cryptoshadow_v10_{ledger}_{stamp}.zip"


def export_all_ledgers_zip(
    config: dict[str, Any], root: Path, output_path: Path,
) -> dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "format": "cryptoshadow-all-ledgers-v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "sample_started_at": config.get("sample_started_at"),
        "management_revision": (config.get("strategy") or {}).get("management_revision"),
        "ledgers": {},
    }
    with tempfile.TemporaryDirectory(prefix="cryptoshadow_all_") as folder:
        temporary = Path(folder)
        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
            for ledger in LEDGER_SPECS:
                ledger_path = temporary / f"{ledger}.zip"
                ledger_manifest = export_ledger_zip(ledger, config, root, ledger_path)
                manifest["ledgers"][ledger] = ledger_manifest["tables"]
                archive.write(ledger_path, f"ledgers/{ledger}.zip")
            archive.writestr(
                "README.txt",
                "CryptoShadow V10 - todos os ledgers para análise\n"
                "Este pacote contém Testnet, Shadow Real, Shadow individual e simulações staged.\n"
                "Envie somente este arquivo ao ChatGPT; cada ledger interno já está sanitizado.\n",
            )
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest
