from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="Cria lote pequeno e deduplicável de mídias prioritárias.")
    parser.add_argument("database", type=Path)
    parser.add_argument("source_root", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("patterns", nargs="+")
    parser.add_argument("--checkpoint-id", required=True)
    args = parser.parse_args()

    root = args.source_root.resolve()
    selected: set[Path] = set()
    for pattern in args.patterns:
        selected.update(path for path in root.glob(pattern) if path.is_file())
    records = []
    for path in sorted(selected, key=lambda item: item.as_posix().lower()):
        records.append({
            "path": path.relative_to(root).as_posix(),
            "sha256": digest(path),
            "bytes": path.stat().st_size,
        })
    now = datetime.now(timezone.utc).isoformat()
    manifest = {
        "checkpoint_id": args.checkpoint_id,
        "generated_utc": now,
        "source_root": str(root),
        "patterns": args.patterns,
        "media": records,
    }
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    connection = sqlite3.connect(args.database)
    connection.execute(
        "INSERT OR REPLACE INTO checkpoints VALUES(?,?,?,?,?)",
        (args.checkpoint_id, now, str(root), "TARGETED_MEDIA", str(args.manifest.resolve())),
    )
    connection.executemany(
        "INSERT OR REPLACE INTO artifacts(checkpoint_id,path,sha256,kind,bytes,status) VALUES(?,?,?,?,?,?)",
        [(args.checkpoint_id, row["path"], row["sha256"], "MEDIA", row["bytes"], "SNAPSHOTTED") for row in records],
    )
    connection.commit()
    connection.close()
    print(json.dumps({"checkpoint_id": args.checkpoint_id, "files": len(records), "bytes": sum(r["bytes"] for r in records)}))


if __name__ == "__main__":
    main()
