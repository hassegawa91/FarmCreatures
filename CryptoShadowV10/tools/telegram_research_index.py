from __future__ import annotations

import argparse
import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit


SPACE_RE = re.compile(r"\s+")
TOPICS = {
    "oi": re.compile(r"\b(?:oi|open\s+interest|interesse\s+aberto)\b", re.IGNORECASE),
    "lsr": re.compile(r"\b(?:lsr|long[ /_-]*short(?:\s+ratio)?)\b", re.IGNORECASE),
    "funding": re.compile(r"\b(?:funding|taxa\s+de\s+financiamento)\b", re.IGNORECASE),
    "liquidation": re.compile(r"\b(?:liquidacao|liquidation|liquidado[rs]?)\b", re.IGNORECASE),
    "absorption": re.compile(r"\b(?:absorcao|absorvendo|absorption)\b", re.IGNORECASE),
    "breakout": re.compile(r"\b(?:rompimento|rompeu|breakout|breakdown)\b", re.IGNORECASE),
    "retest": re.compile(r"\b(?:reteste?|retest|pullback)\b", re.IGNORECASE),
    "reversal": re.compile(r"\b(?:reversao|reversal|exaustao|exhaustion)\b", re.IGNORECASE),
    "flow": re.compile(r"\b(?:taker|delta|order\s*flow|fluxo|agressao)\b", re.IGNORECASE),
    "volume": re.compile(r"\bvolume\b", re.IGNORECASE),
    "risk": re.compile(r"\b(?:stop|stopp?ou|stop\s*loss|take\s*profit|\btp\b|\bsl\b|risco)\b", re.IGNORECASE),
    "signal": re.compile(r"\b(?:entrada|sinal|signal|long|short|compra|venda)\b", re.IGNORECASE),
}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


def clean(value: str) -> str:
    return SPACE_RE.sub(" ", value).strip()


def topic_hits(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKD", text)
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return [name for name, pattern in TOPICS.items() if pattern.search(normalized)]


class TelegramPageParser(HTMLParser):
    def __init__(self, inherited_author: str | None = None) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, set[str]]] = []
        self.current: dict[str, object] | None = None
        self.records: list[dict[str, object]] = []
        self.last_author = inherited_author
        self.sequence = 0

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = {key: value or "" for key, value in attrs_list}
        classes = set(attrs.get("class", "").split())
        if tag not in VOID_TAGS:
            self.stack.append((tag, classes))

        if tag == "div" and "message" in classes and self.current is None:
            self.sequence += 1
            self.current = {
                "ordinal": self.sequence,
                "source_message_id": attrs.get("id") or "",
                "timestamp": "",
                "author_parts": [],
                "text_parts": [],
                "links": [],
                "media": [],
                "root_depth": len(self.stack),
                "service": "service" in classes,
            }
            return

        if self.current is None:
            return
        if tag == "div" and "date" in classes and attrs.get("title"):
            self.current["timestamp"] = attrs["title"]
        if tag == "a" and attrs.get("href"):
            href = attrs["href"]
            cast_list = self.current["links"]
            assert isinstance(cast_list, list)
            cast_list.append(href)
            parsed = urlsplit(href)
            if not parsed.scheme and not parsed.netloc and parsed.path and not parsed.path.lower().endswith(".html"):
                media = self.current["media"]
                assert isinstance(media, list)
                media.append(parsed.path.replace("\\", "/"))

    def handle_data(self, data: str) -> None:
        if self.current is None or not data.strip():
            return
        ancestors = [classes for _, classes in self.stack]
        if any("from_name" in classes for classes in ancestors):
            parts = self.current["author_parts"]
            assert isinstance(parts, list)
            parts.append(data)
        if any("text" in classes for classes in ancestors):
            parts = self.current["text_parts"]
            assert isinstance(parts, list)
            parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID_TAGS:
            return
        if self.current is not None and tag == "div" and len(self.stack) == self.current["root_depth"]:
            author = clean(" ".join(self.current.pop("author_parts")))
            if author:
                self.last_author = author
            text = clean(" ".join(self.current.pop("text_parts")))
            self.current["author"] = author or self.last_author or ""
            self.current["text"] = text
            self.current["topics"] = topic_hits(text)
            self.current["links"] = list(dict.fromkeys(self.current["links"]))
            self.current["media"] = list(dict.fromkeys(self.current["media"]))
            self.current.pop("root_depth", None)
            self.records.append(self.current)
            self.current = None
        if self.stack:
            self.stack.pop()


def schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;
        CREATE TABLE IF NOT EXISTS checkpoints (
            checkpoint_id TEXT PRIMARY KEY,
            generated_utc TEXT NOT NULL,
            source_root TEXT NOT NULL,
            last_page TEXT NOT NULL,
            manifest_path TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS artifacts (
            checkpoint_id TEXT NOT NULL,
            path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            kind TEXT NOT NULL,
            bytes INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'SNAPSHOTTED',
            PRIMARY KEY (checkpoint_id, path)
        );
        CREATE INDEX IF NOT EXISTS artifacts_hash ON artifacts(sha256);
        CREATE TABLE IF NOT EXISTS processed_hashes (
            sha256 TEXT NOT NULL,
            stage TEXT NOT NULL,
            processed_utc TEXT NOT NULL,
            checkpoint_id TEXT NOT NULL,
            path TEXT NOT NULL,
            item_count INTEGER NOT NULL,
            PRIMARY KEY (sha256, stage)
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY,
            checkpoint_id TEXT NOT NULL,
            page_path TEXT NOT NULL,
            page_sha256 TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            source_message_id TEXT,
            timestamp_raw TEXT,
            author TEXT,
            text TEXT,
            topics TEXT NOT NULL,
            media_json TEXT NOT NULL,
            links_json TEXT NOT NULL,
            service INTEGER NOT NULL DEFAULT 0,
            UNIQUE(checkpoint_id, page_path, ordinal)
        );
        CREATE INDEX IF NOT EXISTS messages_timestamp ON messages(timestamp_raw);
        CREATE INDEX IF NOT EXISTS messages_source_id ON messages(source_message_id);
        CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(
            message_rowid UNINDEXED, text, author, topics, tokenize='unicode61 remove_diacritics 2'
        );
        """
    )


def load_manifest(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def index_checkpoint(manifest_path: Path, database_path: Path, ledger_path: Path) -> dict[str, int]:
    manifest = load_manifest(manifest_path)
    checkpoint_id = str(manifest["checkpoint_id"])
    source_root = Path(str(manifest["source"]["export_root"]))
    pages = manifest["pages"]
    media = manifest["media"]
    assert isinstance(pages, list) and isinstance(media, list)

    database_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.touch(exist_ok=True)
    connection = sqlite3.connect(database_path)
    schema(connection)
    now = datetime.now(timezone.utc).isoformat()
    connection.execute(
        "INSERT OR REPLACE INTO checkpoints VALUES(?,?,?,?,?)",
        (checkpoint_id, manifest["generated_utc"], str(source_root), manifest["scope"]["last_page"], str(manifest_path.resolve())),
    )
    artifact_rows = [
        (checkpoint_id, item["path"], item["sha256"], "PAGE", item["bytes"], "SNAPSHOTTED")
        for item in pages
    ] + [
        (checkpoint_id, item["path"], item["sha256"], "MEDIA", item["bytes"], "SNAPSHOTTED")
        for item in media
    ]
    connection.executemany(
        "INSERT OR IGNORE INTO artifacts(checkpoint_id,path,sha256,kind,bytes,status) VALUES(?,?,?,?,?,?)",
        artifact_rows,
    )
    connection.commit()

    indexed_pages = 0
    skipped_pages = 0
    indexed_messages = 0
    last_author: str | None = None
    ledger_entries: list[dict[str, object]] = []
    for page in pages:
        page_hash = str(page["sha256"])
        prior = connection.execute(
            "SELECT 1 FROM processed_hashes WHERE sha256=? AND stage='TEXT_INDEXED'",
            (page_hash,),
        ).fetchone()
        if prior:
            skipped_pages += 1
            continue
        page_path = source_root / str(page["path"])
        parser = TelegramPageParser(last_author)
        parser.feed(page_path.read_text(encoding="utf-8", errors="replace"))
        last_author = parser.last_author
        rows = []
        for item in parser.records:
            rows.append((
                checkpoint_id, page["path"], page_hash, item["ordinal"], item["source_message_id"],
                item["timestamp"], item["author"], item["text"], ",".join(item["topics"]),
                json.dumps(item["media"], ensure_ascii=False), json.dumps(item["links"], ensure_ascii=False),
                int(bool(item["service"])),
            ))
        cursor = connection.executemany(
            """INSERT OR IGNORE INTO messages(
                checkpoint_id,page_path,page_sha256,ordinal,source_message_id,timestamp_raw,
                author,text,topics,media_json,links_json,service
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        message_ids = connection.execute(
            "SELECT id,text,author,topics FROM messages WHERE checkpoint_id=? AND page_path=? ORDER BY ordinal",
            (checkpoint_id, page["path"]),
        ).fetchall()
        connection.executemany(
            "INSERT INTO message_fts(message_rowid,text,author,topics) VALUES(?,?,?,?)",
            message_ids,
        )
        processed_at = datetime.now(timezone.utc).isoformat()
        connection.execute(
            "INSERT INTO processed_hashes VALUES(?,?,?,?,?,?)",
            (page_hash, "TEXT_INDEXED", processed_at, checkpoint_id, page["path"], len(parser.records)),
        )
        connection.execute(
            "UPDATE artifacts SET status='TEXT_INDEXED' WHERE checkpoint_id=? AND path=?",
            (checkpoint_id, page["path"]),
        )
        connection.commit()
        ledger_entries.append({
            "sha256": page_hash, "stage": "TEXT_INDEXED", "processed_utc": processed_at,
            "checkpoint_id": checkpoint_id, "path": page["path"], "item_count": len(parser.records),
        })
        indexed_pages += 1
        indexed_messages += len(parser.records)

    if ledger_entries:
        with ledger_path.open("a", encoding="utf-8") as ledger:
            for entry in ledger_entries:
                ledger.write(json.dumps(entry, ensure_ascii=False) + "\n")
    connection.close()
    return {
        "indexed_pages": indexed_pages,
        "skipped_pages": skipped_pages,
        "indexed_messages": indexed_messages,
        "snapshotted_media": len(media),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Indexa um checkpoint do Telegram sem repetir hashes processados")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("database", type=Path)
    parser.add_argument("ledger", type=Path)
    args = parser.parse_args()
    print(json.dumps(index_checkpoint(args.manifest, args.database, args.ledger), ensure_ascii=False))


if __name__ == "__main__":
    main()
