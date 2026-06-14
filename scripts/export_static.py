#!/usr/bin/env python3
"""Export the Zhongwen app as a complete static site.

The export keeps the SQLite-backed FastAPI app as the source of truth, but writes
browser-consumable JSON shards plus a static Vite build under static/.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "zhongwen.sqlite"
DEFAULT_OUT = ROOT / "static"
SHARD_COUNT = 256


def fnv1a_32(value: str) -> int:
    h = 0x811C9DC5
    for byte in value.encode("utf-8"):
        h ^= byte
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


def hash_shard(value: str) -> str:
    return f"{fnv1a_32(value) % SHARD_COUNT:02x}"


def compact_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def write_json(path: Path, data: Any) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = compact_json(data)
    path.write_text(text, encoding="utf-8")
    return len(text.encode("utf-8"))


def rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params)]


def candidate_texts(conn: sqlite3.Connection, limit: int | None) -> list[str]:
    texts: list[str] = []
    seen: set[str] = set()
    sql = """
        SELECT text FROM entry_index
        UNION SELECT char AS text FROM characters
        UNION SELECT traditional AS text FROM words
        UNION SELECT simplified AS text FROM words
        ORDER BY text
    """
    for row in conn.execute(sql):
        text = row[0]
        if not text or text in seen:
            continue
        seen.add(text)
        texts.append(text)
        if limit is not None and len(texts) >= limit:
            break
    return texts


def segmenter_groups(conn: sqlite3.Connection) -> dict[str, dict[str, list[str]]]:
    by_first: dict[str, set[str]] = defaultdict(set)
    for row in conn.execute("SELECT term FROM segmenter_terms ORDER BY term"):
        term = row[0]
        if not term:
            continue
        first = term[0]
        if "\u3400" <= first <= "\u9fff" or 0x20000 <= ord(first) <= 0x323AF:
            by_first[first].add(term)

    shards: dict[str, dict[str, list[str]]] = {f"{i:02x}": {} for i in range(SHARD_COUNT)}
    for first, terms in sorted(by_first.items()):
        shards[hash_shard(first)][first] = sorted(terms, key=lambda item: (-len(item), item))
    return shards


def run_vite_build(out_dir: Path) -> None:
    env = os.environ.copy()
    env["VITE_STATIC_DATA"] = "1"
    subprocess.run(
        ["npm", "run", "build", "--", "--outDir", str(out_dir), "--emptyOutDir"],
        cwd=ROOT / "web",
        env=env,
        check=True,
    )


def add_sharded(shards: dict[str, dict[str, Any]], text: str, payload: Any) -> None:
    shards[hash_shard(text)][text] = payload


def merge_shards(target: dict[str, dict[str, Any]], source: dict[str, dict[str, Any]]) -> None:
    for shard, values in source.items():
        target[shard].update(values)


def chunked(items: list[str], chunk_size: int) -> list[list[str]]:
    return [items[i:i + chunk_size] for i in range(0, len(items), chunk_size)]


def patch_api_for_conn(api_main: Any, conn: sqlite3.Connection) -> None:
    api_main.rows = lambda sql, params=(): rows(conn, sql, params)
    api_main.one = lambda sql, params=(): (dict(row) if (row := conn.execute(sql, params).fetchone()) else None)


def format_duration(seconds: float | None) -> str:
    if seconds is None or seconds == float("inf"):
        return "?:??"
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:d}:{secs:02d}"


def print_progress(done: int, total: int, started_at: float, final: bool = False) -> None:
    elapsed = time.monotonic() - started_at
    rate = done / elapsed if elapsed > 0 else 0
    remaining = (total - done) / rate if rate > 0 else None
    percent = (done / total * 100) if total else 100
    end = "\n" if final else "\r"
    print(
        f"exported {done}/{total} entries "
        f"({percent:5.1f}%) "
        f"elapsed {format_duration(elapsed)} "
        f"rate {rate:,.1f}/s "
        f"ETA {format_duration(remaining)}",
        end=end,
        file=sys.stderr,
        flush=True,
    )


def related_words_for(conn: sqlite3.Connection, api_main: Any, text: str) -> list[dict[str, Any]]:
    related = rows(
        conn,
        """
        SELECT id, traditional, simplified, pinyin_diacritic, definitions_json, source
        FROM words
        WHERE traditional LIKE ? OR simplified LIKE ?
        ORDER BY length(traditional), traditional
        LIMIT 80
        """,
        (f"%{text}%", f"%{text}%"),
    )
    return [api_main.word_dict(row) for row in related]


def export_entry_chunk(db_path: str, texts: list[str], include_containing: bool) -> dict[str, dict[str, dict[str, Any]]]:
    os.environ["ZHONGWEN_DB"] = db_path
    sys.path.insert(0, str(ROOT))
    from api import main as api_main

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    patch_api_for_conn(api_main, conn)

    entry_shards: dict[str, dict[str, Any]] = {f"{i:02x}": {} for i in range(SHARD_COUNT)}
    summary_shards: dict[str, dict[str, Any]] = {f"{i:02x}": {} for i in range(SHARD_COUNT)}
    character_shards: dict[str, dict[str, Any]] = {f"{i:02x}": {} for i in range(SHARD_COUNT)}
    word_shards: dict[str, dict[str, Any]] = {f"{i:02x}": {} for i in range(SHARD_COUNT)}
    containing_shards: dict[str, dict[str, Any]] = {f"{i:02x}": {} for i in range(SHARD_COUNT)}

    for text in texts:
        summary = api_main.entry_summary(text)
        add_sharded(summary_shards, text, summary)
        add_sharded(entry_shards, text, {"text": text, "summary": summary})

        if len(text) == 1:
            character = api_main.one("SELECT * FROM characters WHERE char=?", (text,))
            if character:
                add_sharded(character_shards, text, api_main.char_detail(text))
        word_rows = [
            api_main.word_dict(row)
            for row in api_main.rows("SELECT * FROM words WHERE traditional=? OR simplified=? ORDER BY id", (text, text))
        ]
        if word_rows:
            word_readings = api_main.with_zhuyin_readings(api_main.word_readings_for_words(word_rows, text))
            add_sharded(word_shards, text, {
                "text": text,
                "words": word_rows,
                "word_readings": word_readings,
                "readings": word_readings,
                "characters": [api_main.entry_summary(ch) for ch in text] if len(text) > 1 else [],
                "hsk": api_main.hsk_for_text(text),
            })
        if include_containing:
            containing = related_words_for(conn, api_main, text)
            if containing:
                add_sharded(containing_shards, text, containing)

    conn.close()
    return {
        "entries": entry_shards,
        "summaries": summary_shards,
        "characters": character_shards,
        "words": word_shards,
        "containing": containing_shards,
    }


def export_data(db_path: Path, out_dir: Path, limit: int | None, workers: int, chunk_size: int, include_containing: bool) -> dict[str, Any]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    texts = candidate_texts(conn, limit)

    entry_shards: dict[str, dict[str, Any]] = {f"{i:02x}": {} for i in range(SHARD_COUNT)}
    summary_shards: dict[str, dict[str, Any]] = {f"{i:02x}": {} for i in range(SHARD_COUNT)}
    character_shards: dict[str, dict[str, Any]] = {f"{i:02x}": {} for i in range(SHARD_COUNT)}
    word_shards: dict[str, dict[str, Any]] = {f"{i:02x}": {} for i in range(SHARD_COUNT)}
    containing_shards: dict[str, dict[str, Any]] = {f"{i:02x}": {} for i in range(SHARD_COUNT)}

    chunks = chunked(texts, chunk_size)
    completed = 0
    started_at = time.monotonic()
    print_progress(0, len(texts), started_at)
    if workers == 1 or len(chunks) <= 1:
        for chunk in chunks:
            result = export_entry_chunk(str(db_path), chunk, include_containing)
            merge_shards(entry_shards, result["entries"])
            merge_shards(summary_shards, result["summaries"])
            merge_shards(character_shards, result["characters"])
            merge_shards(word_shards, result["words"])
            merge_shards(containing_shards, result["containing"])
            completed += len(chunk)
            print_progress(completed, len(texts), started_at, completed >= len(texts))
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(export_entry_chunk, str(db_path), chunk, include_containing) for chunk in chunks]
            for future in as_completed(futures):
                result = future.result()
                merge_shards(entry_shards, result["entries"])
                merge_shards(summary_shards, result["summaries"])
                merge_shards(character_shards, result["characters"])
                merge_shards(word_shards, result["words"])
                merge_shards(containing_shards, result["containing"])
                completed += sum(len(values) for values in result["entries"].values())
                print_progress(completed, len(texts), started_at, completed >= len(texts))

    bytes_written = 0
    for shard, payload in summary_shards.items():
        bytes_written += write_json(out_dir / "data" / "summaries" / f"{shard}.json", payload)
    for shard, payload in entry_shards.items():
        bytes_written += write_json(out_dir / "data" / "entries" / f"{shard}.json", payload)
    for shard, payload in character_shards.items():
        bytes_written += write_json(out_dir / "data" / "characters" / f"{shard}.json", payload)
    for shard, payload in word_shards.items():
        bytes_written += write_json(out_dir / "data" / "words" / f"{shard}.json", payload)
    for shard, payload in containing_shards.items():
        bytes_written += write_json(out_dir / "data" / "containing" / f"{shard}.json", payload)

    segmenter = segmenter_groups(conn)
    segmenter_manifest = {}
    for shard, groups in segmenter.items():
        rel = f"data/segmenter/{shard}.json"
        term_count = sum(len(terms) for terms in groups.values())
        bytes_written += write_json(out_dir / rel, groups)
        segmenter_manifest[shard] = {"path": rel, "first_chars": len(groups), "terms": term_count}

    source_count = conn.execute("SELECT count(*) FROM sources").fetchone()[0]
    manifest = {
        "version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db_path": str(db_path.relative_to(ROOT) if db_path.is_relative_to(ROOT) else db_path),
        "shard_count": SHARD_COUNT,
        "limited": limit is not None,
        "entries": len(texts),
        "sources": source_count,
        "paths": {
            "entries": "data/entries/{shard}.json",
            "summaries": "data/summaries/{shard}.json",
            "characters": "data/characters/{shard}.json",
            "words": "data/words/{shard}.json",
            "containing": "data/containing/{shard}.json",
            "segmenter": "data/segmenter/{hashShard(firstChar)}.json",
        },
        "segmenter": segmenter_manifest,
    }
    bytes_written += write_json(out_dir / "data" / "manifest.json", manifest)

    report = {
        "entries": len(texts),
        "summary_shards": SHARD_COUNT,
        "entry_shards": SHARD_COUNT,
        "character_shards": SHARD_COUNT,
        "word_shards": SHARD_COUNT,
        "containing_shards": SHARD_COUNT,
        "segmenter_shards": len(segmenter),
        "workers": workers,
        "chunk_size": chunk_size,
        "include_containing": include_containing,
        "json_bytes": bytes_written,
    }
    write_json(out_dir / "data" / "export-report.json", report)
    conn.close()
    return report


def default_workers() -> int:
    return max(1, min((os.cpu_count() or 2) - 1, 8))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export Zhongwen as a static site")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite database path")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Output directory")
    parser.add_argument("--limit", type=int, default=None, help="Development-only limit for exported entries")
    parser.add_argument("--skip-vite", action="store_true", help="Only export data, preserving existing static assets")
    parser.add_argument("--workers", type=int, default=default_workers(), help="Entry export worker processes")
    parser.add_argument("--chunk-size", type=int, default=500, help="Texts assigned to each worker task")
    parser.add_argument("--no-containing", action="store_true", help="Skip containing-word shards for faster/smaller exports")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    db_path = args.db.resolve()
    out_dir = args.out.resolve()
    if not db_path.exists():
        raise SystemExit(f"Database not found: {db_path}")
    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit must be positive")
    if args.workers < 1:
        raise SystemExit("--workers must be positive")
    if args.chunk_size < 1:
        raise SystemExit("--chunk-size must be positive")

    if out_dir.exists() and not args.skip_vite:
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not args.skip_vite:
        run_vite_build(out_dir)
    data_dir = out_dir / "data"
    if data_dir.exists():
        shutil.rmtree(data_dir)
    report = export_data(db_path, out_dir, args.limit, args.workers, args.chunk_size, not args.no_containing)
    print(compact_json(report))


if __name__ == "__main__":
    main()
