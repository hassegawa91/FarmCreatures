import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from engine.ledger_export import export_all_ledgers_zip, export_ledger_zip, sanitize


class LedgerExportTests(unittest.TestCase):
    def test_sanitize_redacts_nested_credentials(self):
        value = {"execution": {"api_key": "abc", "password": "def", "enabled": True}}
        clean = sanitize(value)
        self.assertEqual(clean["execution"]["api_key"], "[REDACTED]")
        self.assertEqual(clean["execution"]["password"], "[REDACTED]")
        self.assertTrue(clean["execution"]["enabled"])

    def test_testnet_export_contains_decoded_rows_and_sanitized_config(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            database = root / "ledger.sqlite"
            db = sqlite3.connect(database)
            db.execute("CREATE TABLE signals(id INTEGER PRIMARY KEY, symbol TEXT, evidence_json TEXT)")
            db.execute(
                "INSERT INTO signals(symbol,evidence_json) VALUES(?,?)",
                ("BTCUSDT", json.dumps({"score": 7, "api_secret": "never-export"})),
            )
            db.commit()
            db.close()
            output = root / "export.zip"
            config = {
                "database_path": "ledger.sqlite", "sample_started_at": "2026-08-10",
                "execution": {"api_key": "never-export"},
                "campaign": {"management_revision": "TEST_REV"},
            }
            manifest = export_ledger_zip("testnet", config, root, output)
            self.assertEqual(manifest["management_revision"], "TEST_REV")
            self.assertEqual(manifest["tables"]["signals"]["rows"], 1)
            with zipfile.ZipFile(output) as archive:
                row = json.loads(archive.read("tables/signals.jsonl").decode().strip())
                saved_config = json.loads(archive.read("config.sanitized.json"))
            self.assertEqual(row["evidence"]["score"], 7)
            self.assertEqual(row["evidence"]["api_secret"], "[REDACTED]")
            self.assertEqual(saved_config["execution"]["api_key"], "[REDACTED]")

    def test_unknown_ledger_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                export_ledger_zip("invalid", {}, Path(folder), Path(folder) / "x.zip")

    def test_all_export_bundles_each_sanitized_ledger(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            specs = {
                "testnet.sqlite": "CREATE TABLE signals(id INTEGER PRIMARY KEY)",
                "shadow.sqlite": "CREATE TABLE real_shadow_trades(id INTEGER PRIMARY KEY)",
                "limited.sqlite": "CREATE TABLE real_shadow_trades(id INTEGER PRIMARY KEY)",
                "simulations.sqlite": "CREATE TABLE simulation_trades(id INTEGER PRIMARY KEY)",
            }
            for name, ddl in specs.items():
                db = sqlite3.connect(root / name)
                db.execute(ddl)
                db.commit()
                db.close()
            config = {
                "database_path": "testnet.sqlite",
                "real_shadow": {"database_path": "shadow.sqlite"},
                "limited_shadow": {"database_path": "limited.sqlite"},
                "simulation_lab": {"database_path": "simulations.sqlite"},
            }
            output = root / "all.zip"
            export_all_ledgers_zip(config, root, output)
            with zipfile.ZipFile(output) as archive:
                names = set(archive.namelist())
            self.assertTrue({
                "ledgers/testnet.zip", "ledgers/shadow.zip", "ledgers/limited.zip",
                "ledgers/simulations.zip", "manifest.json",
            }.issubset(names))


if __name__ == "__main__":
    unittest.main()
