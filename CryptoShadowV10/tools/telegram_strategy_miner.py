from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


SPACE = re.compile(r"\s+")
URL = re.compile(r"https?://\S+", re.I)
RULE_WORDS = re.compile(
    r"\b(?:se|quando|esper(?:a|ar|e)|confirm(?:a|ar|acao)|romp(?:e|er|imento)|"
    r"perd(?:e|er)|fech(?:a|ar|amento)|acima|abaixo|sub(?:e|ir|indo)|"
    r"desc(?:e|er|endo)|stop|alvo|reteste|pullback|entrada|sair|saida)\b",
    re.I,
)
TIMEFRAME = re.compile(r"\b(?:[1-9][0-9]?[mhd]|(?:5|15|30|60)\s*min|diario|semanal|mensal)\b", re.I)
PROMO = re.compile(
    r"\b(?:vip|premium|mentoria|curso|inscri(?:cao|va)|cadastre|link na bio|"
    r"grupo dos milionarios|chave do bot|parabens|feliz aniversario|sorteio|"
    r"bem vindo|compilado de estudos|veja os videos|nosso canal)\b",
    re.I,
)
OUTCOME = re.compile(r"\b(?:gain|take|bateu alvo|alvo batido|stopp?ou|loss|lucro|resultado|explodiu)\b", re.I)
EDUCATION = re.compile(r"\b(?:setup|estud|aula|explic|conceito|regra|gerenciamento|como operar|resumindo)\b", re.I)
SIGNAL = re.compile(r"\b(?:possivel|entrada|sinal|long|short|compra|venda|stop|alvo)\b", re.I)

TOPIC_PATTERNS = {
    "structure": re.compile(r"\b(?:estrutura|acumulacao|caixote|triangulo|range|trendline|linha de tendencia|bos|choch|pivo|topo|fundo)\b", re.I),
    "breakout": re.compile(r"\b(?:rompimento|rompeu|breakout|breakdown|perdeu suporte|rompeu resistencia)\b", re.I),
    "oi": re.compile(r"\b(?:oi|open interest|interesse aberto|contratos futuros)\b", re.I),
    "lsr": re.compile(r"\b(?:lsr|long short ratio|long/short|jacaroa|jacare)\b", re.I),
    "flow": re.compile(r"\b(?:taker|delta|fluxo|agressao|negocios por minuto|transacoes por minuto)\b", re.I),
    "volume": re.compile(r"\bvolume\b", re.I),
    "liquidity": re.compile(r"\b(?:liquidez|liquidacao|sweep|varredura|equal highs|equal lows|fvg|order block)\b", re.I),
    "risk": re.compile(r"\b(?:stop|take profit|tp|sl|risco|invalidacao|parcial|breakeven|trailing)\b", re.I),
    "funding": re.compile(r"\b(?:funding|taxa de financiamento)\b", re.I),
    "reversal": re.compile(r"\b(?:reversao|exaustao|pullback|reteste|retorno)\b", re.I),
}


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(char for char in text if not unicodedata.combining(char))
    return SPACE.sub(" ", text).strip().lower()


def fingerprint(text: str) -> str:
    canonical = URL.sub("<url>", normalize(text))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def clipped(text: str, size: int = 420) -> str:
    value = SPACE.sub(" ", text or "").strip()
    return value if len(value) <= size else value[: size - 1] + "…"


@dataclass
class Evidence:
    source_kind: str
    source_id: str
    timestamp: str
    author: str
    source_path: str
    text: str
    topics: list[str]
    category: str
    score: int
    duplicate_count: int = 1

    def as_dict(self) -> dict[str, object]:
        return {
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "timestamp": self.timestamp,
            "author": self.author,
            "source_path": self.source_path,
            "topics": self.topics,
            "category": self.category,
            "evidence_score": self.score,
            "duplicate_count": self.duplicate_count,
            "text": self.text,
        }


def classify(text: str) -> tuple[list[str], str, int]:
    value = normalize(text)
    topics = [name for name, pattern in TOPIC_PATTERNS.items() if pattern.search(value)]
    promo = bool(PROMO.search(value))
    outcome = bool(OUTCOME.search(value))
    education = bool(EDUCATION.search(value))
    signal = bool(SIGNAL.search(value))
    rules = len(RULE_WORDS.findall(value))

    if promo:
        category = "PROMO_NOISE"
    elif education or rules >= 3:
        category = "EDUCATION_RULE"
    elif signal and outcome:
        category = "SIGNAL_OUTCOME"
    elif signal:
        category = "SIGNAL"
    elif outcome:
        category = "OUTCOME_HINDSIGHT"
    else:
        category = "CONVERSATION"

    score = min(len(topics), 4)
    score += min(rules // 2, 3)
    score += 1 if TIMEFRAME.search(value) else 0
    score += 1 if "risk" in topics else 0
    score += 2 if category == "EDUCATION_RULE" else 0
    score -= 3 if category == "PROMO_NOISE" else 0
    score -= 1 if category in {"OUTCOME_HINDSIGHT", "SIGNAL_OUTCOME"} else 0
    score -= 2 if len(value) < 45 else 0
    return topics, category, score


def load_sources(connection: sqlite3.Connection):
    for row in connection.execute(
        """SELECT COALESCE(source_message_id, CAST(id AS TEXT)),
                  COALESCE(timestamp_raw,''), COALESCE(author,''),
                  COALESCE(page_path,''), COALESCE(text,'')
           FROM messages WHERE service=0 AND length(trim(COALESCE(text,'')))>0"""
    ):
        yield "MESSAGE", *row
    has_media = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='media_analysis'"
    ).fetchone()
    if not has_media:
        return
    for row in connection.execute(
        """SELECT sha256, processed_utc, '', source_path, extracted_text
           FROM media_analysis
           WHERE status='OK' AND length(trim(extracted_text))>0
             AND stage IN ('DOCUMENT_TEXT','TRANSCRIPT_SMALL','IMAGE_OCR','VIDEO_FRAME_OCR')"""
    ):
        sha256, processed_utc, author, source_path, text = row
        if "===== PAGE " in text:
            chunks = re.split(r"\n*===== PAGE (\d+) =====\n?", text)
            for index in range(1, len(chunks), 2):
                page = chunks[index]
                page_text = chunks[index + 1].strip() if index + 1 < len(chunks) else ""
                if page_text:
                    yield "MEDIA", f"{sha256}:page:{page}", processed_utc, author, f"{source_path}#page={page}", page_text
        else:
            yield "MEDIA", sha256, processed_utc, author, source_path, text


def mine(connection: sqlite3.Connection) -> list[Evidence]:
    by_fingerprint: dict[str, Evidence] = {}
    for kind, source_id, timestamp, author, source_path, text in load_sources(connection):
        topics, category, score = classify(text)
        item = Evidence(kind, source_id, timestamp, author, source_path, text, topics, category, score)
        key = fingerprint(text)
        existing = by_fingerprint.get(key)
        if existing is None:
            by_fingerprint[key] = item
        else:
            existing.duplicate_count += 1
            if item.score > existing.score:
                item.duplicate_count = existing.duplicate_count
                by_fingerprint[key] = item
    return sorted(by_fingerprint.values(), key=lambda item: (-item.score, item.timestamp, item.source_id))


def write_jsonl(path: Path, evidence: list[Evidence]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for item in evidence:
            handle.write(json.dumps(item.as_dict(), ensure_ascii=False) + "\n")


def write_report(path: Path, evidence: list[Evidence], database: Path) -> None:
    category_counts = Counter(item.category for item in evidence)
    topic_counts = Counter(topic for item in evidence for topic in item.topics)
    high_quality = [item for item in evidence if item.score >= 6 and item.category == "EDUCATION_RULE"]
    clusters: dict[str, list[Evidence]] = defaultdict(list)
    for item in high_quality:
        for topic in item.topics:
            clusters[topic].append(item)

    lines = [
        "# Base de evidências de estratégia - Telegram Encryptos",
        "",
        f"Gerado em: {datetime.now(timezone.utc).isoformat()}",
        f"Banco: `{database}`",
        "",
        "Esta base separa ensinamentos testáveis de sinais, resultados retrospectivos,",
        "conversa e propaganda. Classificação é heurística; promoção para a engine exige",
        "validação quantitativa independente, custos e teste fora da amostra.",
        "",
        "## Resumo",
        "",
        f"- Itens únicos analisados: {len(evidence):,}",
        f"- Regras educacionais com score >= 6: {len(high_quality):,}",
    ]
    for category, count in category_counts.most_common():
        lines.append(f"- `{category}`: {count:,}")
    lines.extend(["", "## Temas encontrados", ""])
    for topic, count in topic_counts.most_common():
        lines.append(f"- `{topic}`: {count:,}")

    lines.extend(["", "## Evidências prioritárias por tema", ""])
    for topic in TOPIC_PATTERNS:
        candidates = []
        seen_stems: set[str] = set()
        for item in clusters.get(topic, []):
            stem = normalize(item.text)[:140]
            if stem in seen_stems:
                continue
            seen_stems.add(stem)
            candidates.append(item)
            if len(candidates) == 8:
                break
        if not candidates:
            continue
        lines.extend([f"### {topic}", ""])
        for item in candidates:
            ref = item.source_id if item.source_kind == "MESSAGE" else item.source_path
            lines.append(
                f"- score {item.score} | `{ref}` | {', '.join(item.topics)} | {clipped(item.text)}"
            )
        lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Minera evidências testáveis do índice Telegram.")
    parser.add_argument("database", type=Path)
    parser.add_argument("jsonl", type=Path)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()

    connection = sqlite3.connect(args.database)
    evidence = mine(connection)
    connection.close()
    write_jsonl(args.jsonl, evidence)
    write_report(args.report, evidence, args.database)
    print(json.dumps({"unique_items": len(evidence), "jsonl": str(args.jsonl), "report": str(args.report)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
