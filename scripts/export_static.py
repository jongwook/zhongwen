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
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

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


def export_data(db_path: Path, out_dir: Path, limit: int | None) -> dict[str, Any]:
    os.environ["ZHONGWEN_DB"] = str(db_path)
    sys.path.insert(0, str(ROOT))
    from api import main as api_main  # Import after ZHONGWEN_DB is set.

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    api_main.rows = lambda sql, params=(): rows(conn, sql, params)
    api_main.one = lambda sql, params=(): (dict(row) if (row := conn.execute(sql, params).fetchone()) else None)
    texts = candidate_texts(conn, limit)

    entry_shards: dict[str, dict[str, Any]] = {f"{i:02x}": {} for i in range(SHARD_COUNT)}
    summary_shards: dict[str, dict[str, Any]] = {f"{i:02x}": {} for i in range(SHARD_COUNT)}
    for index, text in enumerate(texts, 1):
        shard = hash_shard(text)
        summary_shards[shard][text] = api_main.entry_summary(text)
        entry_shards[shard][text] = api_main.entry(text)
        if index % 5000 == 0:
            print(f"exported {index}/{len(texts)} entries", file=sys.stderr)

    bytes_written = 0
    for shard, payload in summary_shards.items():
        bytes_written += write_json(out_dir / "data" / "summaries" / f"{shard}.json", payload)
    for shard, payload in entry_shards.items():
        bytes_written += write_json(out_dir / "data" / "entries" / f"{shard}.json", payload)

    segmenter = segmenter_groups(conn)
    segmenter_manifest = {}
    for shard, groups in segmenter.items():
        rel = f"data/segmenter/{shard}.json"
        term_count = sum(len(terms) for terms in groups.values())
        bytes_written += write_json(out_dir / rel, groups)
        segmenter_manifest[shard] = {"path": rel, "first_chars": len(groups), "terms": term_count}

    source_count = conn.execute("SELECT count(*) FROM sources").fetchone()[0]
    manifest = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db_path": str(db_path.relative_to(ROOT) if db_path.is_relative_to(ROOT) else db_path),
        "shard_count": SHARD_COUNT,
        "limited": limit is not None,
        "entries": len(texts),
        "sources": source_count,
        "paths": {
            "entries": "data/entries/{shard}.json",
            "summaries": "data/summaries/{shard}.json",
            "segmenter": "data/segmenter/{hashShard(firstChar)}.json",
        },
        "segmenter": segmenter_manifest,
    }
    bytes_written += write_json(out_dir / "data" / "manifest.json", manifest)

    report = {
        "entries": len(texts),
        "summary_shards": SHARD_COUNT,
        "entry_shards": SHARD_COUNT,
        "segmenter_shards": len(segmenter),
        "json_bytes": bytes_written,
    }
    write_json(out_dir / "data" / "export-report.json", report)
    conn.close()
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export Zhongwen as a static site")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite database path")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Output directory")
    parser.add_argument("--limit", type=int, default=None, help="Development-only limit for exported entries")
    parser.add_argument("--skip-vite", action="store_true", help="Only export data, preserving existing static assets")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    db_path = args.db.resolve()
    out_dir = args.out.resolve()
    if not db_path.exists():
        raise SystemExit(f"Database not found: {db_path}")
    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit must be positive")

    if out_dir.exists() and not args.skip_vite:
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not args.skip_vite:
        run_vite_build(out_dir)
    data_dir = out_dir / "data"
    if data_dir.exists():
        shutil.rmtree(data_dir)
    report = export_data(db_path, out_dir, args.limit)
    print(compact_json(report))


if __name__ == "__main__":
    main()
