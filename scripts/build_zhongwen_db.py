#!/usr/bin/env python3
"""
Build an SQLite database for Chinese character learning data.

The script intentionally uses only Python's standard library. It downloads
source datasets, parses them into provenance-preserving tables, and creates
indexes suitable for a web app.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import os
import re
import sqlite3
import sys
import unicodedata
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
SOURCES_DIR = DATA_DIR / "sources"
DB_PATH = DATA_DIR / "zhongwen.sqlite"
MANIFEST_PATH = DATA_DIR / "source_manifest.json"


SOURCES = {
    "unihan": {
        "url": "https://www.unicode.org/Public/UCD/latest/ucd/Unihan.zip",
        "path": SOURCES_DIR / "Unihan.zip",
        "license": "Unicode License v3",
    },
    "cc_cedict": {
        "url": "https://www.mdbg.net/chinese/export/cedict/cedict_1_0_ts_utf-8_mdbg.txt.gz",
        "path": SOURCES_DIR / "cedict_1_0_ts_utf-8_mdbg.txt.gz",
        "license": "CC BY-SA 4.0, per CC-CEDICT project",
    },
    "opencc": {
        "url": "https://github.com/BYVoid/OpenCC/archive/refs/heads/master.zip",
        "path": SOURCES_DIR / "OpenCC-master.zip",
        "license": "Apache-2.0",
    },
    "rime_cantonese": {
        "url": "https://github.com/rime/rime-cantonese/archive/refs/heads/master.zip",
        "path": SOURCES_DIR / "rime-cantonese-master.zip",
        "license": "CC BY 4.0 / ODbL for selected upstream files",
    },
    "hsk": {
        "url": "https://raw.githubusercontent.com/drkameleon/complete-hsk-vocabulary/main/complete.json",
        "path": SOURCES_DIR / "hsk_complete.json",
        "license": "See upstream repository",
        "optional": True,
    },
}


UNIHAN_FIELDS = {
    "kDefinition",
    "kMandarin",
    "kCantonese",
    "kSimplifiedVariant",
    "kTraditionalVariant",
    "kSemanticVariant",
    "kSpecializedSemanticVariant",
    "kZVariant",
    "kCompatibilityVariant",
    "kIICore",
    "kTotalStrokes",
    "kRSUnicode",
    "kIRG_GSource",
    "kIRG_TSource",
    "kIRG_HSource",
    "kIRG_JSource",
    "kIRG_KSource",
    "kIRG_KPSource",
    "kIRG_VSource",
}


CJK_RANGES = [
    (0x3400, 0x4DBF, "CJK Unified Ideographs Extension A"),
    (0x4E00, 0x9FFF, "CJK Unified Ideographs"),
    (0xF900, 0xFAFF, "CJK Compatibility Ideographs"),
    (0x20000, 0x2A6DF, "CJK Unified Ideographs Extension B"),
    (0x2A700, 0x2B73F, "CJK Unified Ideographs Extension C"),
    (0x2B740, 0x2B81F, "CJK Unified Ideographs Extension D"),
    (0x2B820, 0x2CEAF, "CJK Unified Ideographs Extension E"),
    (0x2CEB0, 0x2EBEF, "CJK Unified Ideographs Extension F"),
    (0x30000, 0x3134F, "CJK Unified Ideographs Extension G"),
    (0x31350, 0x323AF, "CJK Unified Ideographs Extension H"),
    (0x2EBF0, 0x2EE5F, "CJK Unified Ideographs Extension I"),
]


PINYIN_TONE_MARKS = {
    "a": "āáǎàa",
    "e": "ēéěèe",
    "i": "īíǐìi",
    "o": "ōóǒòo",
    "u": "ūúǔùu",
    "v": "ǖǘǚǜü",
    "ü": "ǖǘǚǜü",
}


@dataclass(frozen=True)
class SourceResult:
    key: str
    url: str
    path: str
    sha256: str | None
    bytes: int | None
    license: str
    status: str


def sha256_path(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def download_sources(force: bool = False) -> list[SourceResult]:
    SOURCES_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    for key, meta in SOURCES.items():
        path = Path(meta["path"])
        status = "cached"
        if force or not path.exists():
            try:
                print(f"Downloading {key}: {meta['url']}", file=sys.stderr)
                with urllib.request.urlopen(meta["url"], timeout=60) as response:
                    path.write_bytes(response.read())
                status = "downloaded"
            except Exception as exc:
                if meta.get("optional"):
                    print(f"Optional source {key} unavailable: {exc}", file=sys.stderr)
                    results.append(
                        SourceResult(
                            key,
                            meta["url"],
                            str(path.relative_to(ROOT)),
                            None,
                            None,
                            meta["license"],
                            f"unavailable: {exc}",
                        )
                    )
                    continue
                raise
        results.append(
            SourceResult(
                key,
                meta["url"],
                str(path.relative_to(ROOT)),
                sha256_path(path),
                path.stat().st_size,
                meta["license"],
                status,
            )
        )
    return results


def connect_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE sources (
            key TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            path TEXT,
            sha256 TEXT,
            bytes INTEGER,
            license TEXT,
            status TEXT NOT NULL
        );

        CREATE TABLE characters (
            codepoint INTEGER PRIMARY KEY,
            char TEXT NOT NULL UNIQUE,
            codepoint_hex TEXT NOT NULL UNIQUE,
            block TEXT NOT NULL,
            is_bmp INTEGER NOT NULL,
            unicode_name TEXT,
            is_iicore INTEGER NOT NULL DEFAULT 0,
            definition TEXT,
            total_strokes TEXT,
            radical_strokes TEXT,
            normalized_nfc TEXT NOT NULL,
            normalized_nfkc TEXT NOT NULL
        );

        CREATE TABLE character_readings (
            char TEXT NOT NULL,
            system TEXT NOT NULL,
            reading TEXT NOT NULL,
            source TEXT NOT NULL,
            PRIMARY KEY (char, system, reading, source)
        );

        CREATE TABLE character_variants (
            char TEXT NOT NULL,
            variant_type TEXT NOT NULL,
            variant TEXT NOT NULL,
            source TEXT NOT NULL,
            raw_value TEXT,
            PRIMARY KEY (char, variant_type, variant, source)
        );

        CREATE TABLE character_sources (
            char TEXT NOT NULL,
            source_field TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY (char, source_field, value)
        );

        CREATE TABLE words (
            id INTEGER PRIMARY KEY,
            traditional TEXT NOT NULL,
            simplified TEXT NOT NULL,
            pinyin_numbered TEXT,
            pinyin_diacritic TEXT,
            definitions_json TEXT NOT NULL,
            source TEXT NOT NULL,
            UNIQUE (traditional, simplified, pinyin_numbered, source)
        );

        CREATE TABLE word_readings (
            word_id INTEGER NOT NULL,
            system TEXT NOT NULL,
            reading TEXT NOT NULL,
            source TEXT NOT NULL,
            PRIMARY KEY (word_id, system, reading, source),
            FOREIGN KEY (word_id) REFERENCES words(id) ON DELETE CASCADE
        );

        CREATE TABLE segmenter_terms (
            term TEXT PRIMARY KEY,
            traditional TEXT,
            simplified TEXT,
            frequency INTEGER NOT NULL DEFAULT 1,
            tag TEXT,
            source TEXT NOT NULL
        );

        CREATE TABLE conversion_mappings (
            source_text TEXT NOT NULL,
            target_text TEXT NOT NULL,
            dictionary TEXT NOT NULL,
            source TEXT NOT NULL,
            PRIMARY KEY (source_text, target_text, dictionary)
        );

        CREATE TABLE hsk_words (
            word TEXT NOT NULL,
            traditional TEXT,
            simplified TEXT,
            level INTEGER NOT NULL,
            hsk_version TEXT NOT NULL,
            source TEXT NOT NULL,
            PRIMARY KEY (word, level, hsk_version, source)
        );

        CREATE TABLE hsk_characters (
            char TEXT NOT NULL,
            level INTEGER NOT NULL,
            hsk_version TEXT NOT NULL,
            source TEXT NOT NULL,
            PRIMARY KEY (char, level, hsk_version, source)
        );

        CREATE TABLE hsk_character_levels (
            char TEXT NOT NULL,
            level INTEGER NOT NULL,
            hsk_version TEXT NOT NULL,
            source TEXT NOT NULL,
            PRIMARY KEY (char, hsk_version, source)
        );

        CREATE INDEX idx_character_readings_reading ON character_readings(system, reading);
        CREATE INDEX idx_character_variants_variant ON character_variants(variant);
        CREATE INDEX idx_words_simplified ON words(simplified);
        CREATE INDEX idx_words_traditional ON words(traditional);
        CREATE INDEX idx_segmenter_frequency ON segmenter_terms(frequency DESC);
        CREATE INDEX idx_hsk_characters_level ON hsk_characters(level);
        """
    )


def block_for(cp: int) -> str | None:
    for start, end, name in CJK_RANGES:
        if start <= cp <= end:
            return name
    return None


def unihan_char(token: str) -> str:
    return chr(int(token[2:], 16))


def parse_unihan_value_variants(value: str) -> list[str]:
    variants = []
    for match in re.finditer(r"U\+([0-9A-Fa-f]{4,6})", value):
        variants.append(chr(int(match.group(1), 16)))
    return variants


def load_unihan(conn: sqlite3.Connection, zip_path: Path) -> None:
    props: dict[str, dict[str, str]] = {}
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if not name.endswith(".txt"):
                continue
            with zf.open(name) as raw:
                for raw_line in io.TextIOWrapper(raw, encoding="utf-8"):
                    line = raw_line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split("\t")
                    if len(parts) != 3:
                        continue
                    cp_token, field, value = parts
                    if field in UNIHAN_FIELDS:
                        props.setdefault(unihan_char(cp_token), {})[field] = value

    for start, end, block in CJK_RANGES:
        for cp in range(start, end + 1):
            try:
                ch = chr(cp)
                name = unicodedata.name(ch, None)
            except ValueError:
                continue
            if name is None and ch not in props:
                continue
            fields = props.get(ch, {})
            conn.execute(
                """
                INSERT OR IGNORE INTO characters
                (codepoint, char, codepoint_hex, block, is_bmp, unicode_name,
                 is_iicore, definition, total_strokes, radical_strokes,
                 normalized_nfc, normalized_nfkc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cp,
                    ch,
                    f"U+{cp:04X}",
                    block,
                    1 if cp <= 0xFFFF else 0,
                    name,
                    1 if fields.get("kIICore") else 0,
                    fields.get("kDefinition"),
                    fields.get("kTotalStrokes"),
                    fields.get("kRSUnicode"),
                    unicodedata.normalize("NFC", ch),
                    unicodedata.normalize("NFKC", ch),
                ),
            )

            for field, system in (("kMandarin", "pinyin"), ("kCantonese", "jyutping")):
                for reading in fields.get(field, "").split():
                    conn.execute(
                        "INSERT OR IGNORE INTO character_readings VALUES (?, ?, ?, ?)",
                        (ch, system, reading, "unihan"),
                    )

            for field, variant_type in (
                ("kSimplifiedVariant", "simplified"),
                ("kTraditionalVariant", "traditional"),
                ("kSemanticVariant", "semantic"),
                ("kSpecializedSemanticVariant", "specialized_semantic"),
                ("kZVariant", "z_variant"),
                ("kCompatibilityVariant", "compatibility"),
            ):
                raw = fields.get(field)
                if not raw:
                    continue
                for variant in parse_unihan_value_variants(raw):
                    conn.execute(
                        "INSERT OR IGNORE INTO character_variants VALUES (?, ?, ?, ?, ?)",
                        (ch, variant_type, variant, "unihan", raw),
                    )

            for field in (
                "kIRG_GSource",
                "kIRG_TSource",
                "kIRG_HSource",
                "kIRG_JSource",
                "kIRG_KSource",
                "kIRG_KPSource",
                "kIRG_VSource",
            ):
                if fields.get(field):
                    conn.execute(
                        "INSERT OR IGNORE INTO character_sources VALUES (?, ?, ?)",
                        (ch, field, fields[field]),
                    )


CEDICT_RE = re.compile(r"^(\S+)\s+(\S+)\s+\[(.*?)\]\s+/(.*)/$")


def pinyin_to_diacritic(s: str) -> str:
    def convert_syllable(match: re.Match[str]) -> str:
        syllable = match.group(1)
        tone = int(match.group(2))
        lower = syllable.replace("u:", "v").replace("U:", "v")
        if tone == 5:
            return lower.replace("v", "ü")
        target_index = -1
        for vowel in ("a", "e"):
            idx = lower.find(vowel)
            if idx >= 0:
                target_index = idx
                break
        if target_index < 0:
            ou = lower.find("ou")
            if ou >= 0:
                target_index = ou
        if target_index < 0:
            for i in range(len(lower) - 1, -1, -1):
                if lower[i] in "aeiouvü":
                    target_index = i
                    break
        if target_index < 0:
            return lower
        vowel = lower[target_index]
        marked = PINYIN_TONE_MARKS[vowel][tone - 1]
        return lower[:target_index] + marked + lower[target_index + 1 :].replace("v", "ü")

    return re.sub(r"([A-Za-züÜ:]+)([1-5])", convert_syllable, s)


def load_cedict(conn: sqlite3.Connection, gz_path: Path) -> None:
    with gzip.open(gz_path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = CEDICT_RE.match(line)
            if not m:
                continue
            trad, simp, pinyin, defs = m.groups()
            definitions = [d for d in defs.split("/") if d]
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO words
                (traditional, simplified, pinyin_numbered, pinyin_diacritic, definitions_json, source)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (trad, simp, pinyin, pinyin_to_diacritic(pinyin), json.dumps(definitions, ensure_ascii=False), "cc_cedict"),
            )
            word_id = cur.lastrowid
            if not word_id:
                row = conn.execute(
                    "SELECT id FROM words WHERE traditional=? AND simplified=? AND pinyin_numbered=? AND source=?",
                    (trad, simp, pinyin, "cc_cedict"),
                ).fetchone()
                word_id = row[0]
            conn.execute(
                "INSERT OR IGNORE INTO word_readings VALUES (?, ?, ?, ?)",
                (word_id, "pinyin", pinyin, "cc_cedict"),
            )
            frequency = max(1, 10_000_000 // max(1, len(simp)))
            for term in {trad, simp}:
                conn.execute(
                    """
                    INSERT INTO segmenter_terms(term, traditional, simplified, frequency, tag, source)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(term) DO UPDATE SET
                        frequency=max(frequency, excluded.frequency)
                    """,
                    (term, trad, simp, frequency, "cedict", "cc_cedict"),
                )


def zip_text_members(zip_path: Path, suffix: str) -> Iterable[tuple[str, str]]:
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if name.endswith(suffix):
                with zf.open(name) as raw:
                    yield name, raw.read().decode("utf-8", errors="replace")


def load_opencc(conn: sqlite3.Connection, zip_path: Path) -> None:
    for name, text in zip_text_members(zip_path, ".txt"):
        if "/data/dictionary/" not in name:
            continue
        dictionary = Path(name).name
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            source_text, targets = parts[0], parts[1]
            for target in targets.split():
                conn.execute(
                    "INSERT OR IGNORE INTO conversion_mappings VALUES (?, ?, ?, ?)",
                    (source_text, target, dictionary, "opencc"),
                )
                if len(source_text) == 1 and len(target) == 1:
                    conn.execute(
                        "INSERT OR IGNORE INTO character_variants VALUES (?, ?, ?, ?, ?)",
                        (source_text, f"opencc:{dictionary}", target, "opencc", line),
                    )


def load_rime_cantonese(conn: sqlite3.Connection, zip_path: Path) -> None:
    for name, text in zip_text_members(zip_path, ".dict.yaml"):
        if "jyut6ping3" not in name:
            continue
        source_name = "rime_cantonese:" + Path(name).name
        in_entries = False
        for line in text.splitlines():
            if line.strip() == "...":
                in_entries = True
                continue
            if not in_entries or not line.strip() or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            word, jyutping = parts[0].strip(), parts[1].strip()
            if not word or not jyutping:
                continue
            if len(word) == 1:
                conn.execute(
                    "INSERT OR IGNORE INTO character_readings VALUES (?, ?, ?, ?)",
                    (word, "jyutping", jyutping, source_name),
                )
            else:
                row = conn.execute(
                    "SELECT id FROM words WHERE simplified=? OR traditional=? LIMIT 1",
                    (word, word),
                ).fetchone()
                if row:
                    conn.execute(
                        "INSERT OR IGNORE INTO word_readings VALUES (?, ?, ?, ?)",
                        (row[0], "jyutping", jyutping, source_name),
                    )
            conn.execute(
                """
                INSERT INTO segmenter_terms(term, frequency, tag, source)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(term) DO UPDATE SET
                    frequency=max(frequency, excluded.frequency)
                """,
                (word, 500_000, "jyutping", source_name),
            )


def infer_hsk_records(obj: object) -> list[tuple[str, str | None, int]]:
    records: list[tuple[str, str | None, int]] = []

    def old_hsk_level(value: object) -> int | None:
        if isinstance(value, str):
            m = re.fullmatch(r"old-([1-6])", value)
            return int(m.group(1)) if m else None
        if isinstance(value, list):
            levels = [old_hsk_level(item) for item in value]
            levels = [level for level in levels if level is not None]
            return min(levels) if levels else None
        return None

    def visit(value: object) -> None:
        if isinstance(value, dict):
            level = old_hsk_level(value.get("level"))
            simplified = value.get("simplified") if isinstance(value.get("simplified"), str) else None
            traditional = None
            forms = value.get("forms")
            if isinstance(forms, list) and forms:
                first = forms[0]
                if isinstance(first, dict) and isinstance(first.get("traditional"), str):
                    traditional = first["traditional"]
            if simplified and level:
                records.append((simplified, traditional, level))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(obj)
    return records

def load_hsk(conn: sqlite3.Connection, path: Path) -> None:
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Skipping HSK source; cannot parse JSON: {exc}", file=sys.stderr)
        return
    records = infer_hsk_records(data)
    for word, traditional, level in records:
        conn.execute(
            "INSERT OR IGNORE INTO hsk_words VALUES (?, ?, ?, ?, ?, ?)",
            (word, traditional, word, level, "HSK 2.0 1-6", "hsk"),
        )
        for ch in word:
            if block_for(ord(ch)):
                conn.execute(
                    "INSERT OR IGNORE INTO hsk_characters VALUES (?, ?, ?, ?)",
                    (ch, level, "HSK 2.0 1-6", "hsk"),
                )
    conn.execute(
        """
        INSERT OR REPLACE INTO hsk_character_levels(char, level, hsk_version, source)
        SELECT char, min(level), hsk_version, source
        FROM hsk_characters
        WHERE hsk_version='HSK 2.0 1-6' AND source='hsk'
        GROUP BY char, hsk_version, source
        """
    )


def write_manifest(results: list[SourceResult]) -> None:
    MANIFEST_PATH.write_text(
        json.dumps(
            {
                "generated_by": "scripts/build_zhongwen_db.py",
                "sources": [r.__dict__ for r in results],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def insert_sources(conn: sqlite3.Connection, results: list[SourceResult]) -> None:
    conn.executemany(
        "INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(r.key, r.url, r.path, r.sha256, r.bytes, r.license, r.status) for r in results],
    )


def print_summary(conn: sqlite3.Connection) -> None:
    for table in (
        "characters",
        "character_readings",
        "character_variants",
        "words",
        "word_readings",
        "segmenter_terms",
        "conversion_mappings",
        "hsk_words",
        "hsk_characters",
        "hsk_character_levels",
    ):
        count = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        print(f"{table}: {count}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force-download", action="store_true")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    args = parser.parse_args()

    results = download_sources(args.force_download)
    conn = connect_db(args.db)
    try:
        create_schema(conn)
        insert_sources(conn, results)
        load_unihan(conn, SOURCES["unihan"]["path"])
        load_cedict(conn, SOURCES["cc_cedict"]["path"])
        load_opencc(conn, SOURCES["opencc"]["path"])
        load_rime_cantonese(conn, SOURCES["rime_cantonese"]["path"])
        load_hsk(conn, SOURCES["hsk"]["path"])
        conn.commit()
        print_summary(conn)
    finally:
        conn.close()
    write_manifest(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
