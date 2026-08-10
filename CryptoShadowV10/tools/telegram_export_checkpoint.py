from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit


MESSAGE_PAGE_RE = re.compile(r"^messages(?:(\d+))?\.html$", re.IGNORECASE)
LOCAL_LINK_RE = re.compile(r"(?:href|src)=\"([^\"]+)\"", re.IGNORECASE)
MESSAGE_RE = re.compile(r'<div class="message\s', re.IGNORECASE)
DATE_RE = re.compile(r'title="(\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}:\d{2} [^"]+)"')
MEDIA_SUFFIXES = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif",
    ".mp4", ".mov", ".mkv", ".webm",
    ".ogg", ".mp3", ".wav", ".m4a",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".txt", ".zip",
}


def page_number(path: Path) -> int:
    match = MESSAGE_PAGE_RE.match(path.name)
    if not match:
        raise ValueError(path.name)
    return int(match.group(1) or 1)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_record(path: Path, root: Path, *, include_hash: bool = True) -> dict[str, object]:
    stat = path.stat()
    record: dict[str, object] = {
        "path": path.relative_to(root).as_posix(),
        "bytes": stat.st_size,
        "modified_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
    }
    if include_hash:
        record["sha256"] = sha256(path)
    return record


def closed_message_pages(root: Path, include_active: bool = False) -> tuple[list[Path], Path | None]:
    pages = sorted(
        (path for path in root.glob("messages*.html") if MESSAGE_PAGE_RE.match(path.name)),
        key=page_number,
    )
    if not pages:
        raise RuntimeError("Nenhuma pagina messages*.html encontrada")

    # Telegram creates the next page before it finishes writing it. A page is
    # considered closed only if another numbered page already exists after it.
    highest_number = max(page_number(path) for path in pages)
    active = next((path for path in pages if page_number(path) == highest_number), None)
    closed = [path for path in pages if path.stat().st_size > 0 and (
        include_active or page_number(path) < highest_number
    )]
    if include_active:
        active = None
    return closed, active


def referenced_media(page: Path, root: Path) -> set[Path]:
    html = page.read_text(encoding="utf-8", errors="replace")
    found: set[Path] = set()
    for raw_link in LOCAL_LINK_RE.findall(html):
        parsed = urlsplit(raw_link)
        if parsed.scheme or parsed.netloc:
            continue
        relative = unquote(parsed.path).replace("/", "\\")
        if not relative or MESSAGE_PAGE_RE.match(Path(relative).name):
            continue
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            continue
        if candidate.suffix.lower() in MEDIA_SUFFIXES:
            found.add(candidate)
    return found


def build_checkpoint(
    export_root: Path, output_dir: Path, source_id: str = "encryptos",
    chat_title: str = "Encryptos", include_active: bool = False,
    defer_media: bool = False,
) -> Path:
    export_root = export_root.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    pages, active_page = closed_message_pages(export_root, include_active)

    page_records: list[dict[str, object]] = []
    media_paths: set[Path] = set()
    message_count = 0
    dates: list[str] = []

    for page in pages:
        html = page.read_text(encoding="utf-8", errors="replace")
        record = file_record(page, export_root)
        record["page_number"] = page_number(page)
        record["message_blocks"] = len(MESSAGE_RE.findall(html))
        page_records.append(record)
        message_count += int(record["message_blocks"])
        dates.extend(DATE_RE.findall(html))
        if not defer_media:
            media_paths.update(referenced_media(page, export_root))

    media_records: list[dict[str, object]] = []
    missing: list[str] = []
    empty: list[str] = []
    for path in sorted(media_paths, key=lambda item: item.as_posix().lower()):
        relative = path.relative_to(export_root).as_posix()
        if not path.exists():
            missing.append(relative)
        elif path.stat().st_size == 0:
            empty.append(relative)
        else:
            media_records.append(file_record(path, export_root))

    generated = datetime.now(timezone.utc)
    checkpoint_id = f"{source_id}-{generated.strftime('%Y%m%dT%H%M%SZ')}-p{page_number(pages[-1])}"
    checkpoint = {
        "schema_version": 1,
        "checkpoint_id": checkpoint_id,
        "generated_utc": generated.isoformat(),
        "source": {
            "export_root": str(export_root),
            "source_id": source_id,
            "chat_title": chat_title,
            "closed_page_rule": "page included only when a later numbered page already exists",
            "active_page_excluded": active_page.name if active_page else None,
        },
        "scope": {
            "first_page": page_records[0]["path"],
            "last_page": page_records[-1]["path"],
            "closed_pages": len(page_records),
            "message_blocks": message_count,
            # Telegram exports pages and messages in chronological order.
            # Do not compare DD.MM.YYYY timestamps lexicographically.
            "first_message_timestamp": dates[0] if dates else None,
            "last_message_timestamp": dates[-1] if dates else None,
            "media_references": len(media_paths),
            "media_present_and_hashed": len(media_records),
            "media_missing": len(missing),
            "media_empty": len(empty),
            "total_hashed_bytes": sum(int(item["bytes"]) for item in page_records + media_records),
        },
        "pages": page_records,
        "media": media_records,
        "missing_media": missing,
        "empty_media": empty,
        "analysis_policy": {
            "deduplication_key": "sha256",
            "process_only_manifested_files": True,
            "later_runs": "compare SHA-256 and process only hashes absent from the analysis ledger",
            "media_deferred": defer_media,
        },
    }

    checkpoint_path = output_dir / f"{checkpoint_id}.json"
    checkpoint_path.write_text(
        json.dumps(checkpoint, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    latest_path = output_dir / "latest_checkpoint.json"
    latest_path.write_text(
        json.dumps(checkpoint, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    ledger_path = output_dir / "analysis_ledger.jsonl"
    ledger_path.touch(exist_ok=True)
    return checkpoint_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Cria checkpoint deduplicavel de exportacao HTML do Telegram")
    parser.add_argument("export_root", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--source-id", default="encryptos")
    parser.add_argument("--chat-title", default="Encryptos")
    parser.add_argument("--include-active", action="store_true", help="Inclui a ultima pagina quando a exportacao terminou")
    parser.add_argument("--defer-media", action="store_true", help="Cria checkpoint textual; midias entram em lotes posteriores")
    args = parser.parse_args()
    print(build_checkpoint(
        args.export_root, args.output_dir, args.source_id, args.chat_title, args.include_active,
        args.defer_media,
    ))


if __name__ == "__main__":
    main()
