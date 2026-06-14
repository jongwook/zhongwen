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
    "kKorean",
    "kJapaneseOn",
    "kJapaneseKun",
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


KOREAN_INITIALS = {
    "KK": 1, "TT": 4, "PP": 8, "SS": 10, "CC": 13,
    "CH": 14, "KH": 15, "TH": 16, "PH": 17,
    "K": 0, "N": 2, "T": 3, "L": 5, "R": 5, "M": 6, "P": 7,
    "S": 9, "C": 12, "H": 18, "": 11,
}
KOREAN_VOWELS = {
    "YAY": 3, "WAY": 10, "WEY": 15, "YEY": 7,
    "YA": 2, "YE": 6, "YO": 12, "YU": 17, "AY": 1, "EY": 5,
    "WA": 9, "WE": 14, "WI": 16, "WU": 13, "OY": 11, "UY": 19,
    "A": 0, "E": 4, "O": 8, "U": 18, "I": 20,
}
KOREAN_FINALS = {
    "": 0, "K": 1, "KK": 2, "KS": 3, "N": 4, "NC": 5, "NH": 6, "T": 7,
    "L": 8, "LK": 9, "LM": 10, "LP": 11, "LS": 12, "LT": 13, "LPH": 14, "LH": 15,
    "M": 16, "P": 17, "PS": 18, "S": 19, "SS": 20, "NG": 21, "C": 22, "CH": 23,
    "KH": 24, "TH": 25, "PH": 26, "H": 27,
}

JAPANESE_ROMAJI = {
    "kya": "きゃ", "kyu": "きゅ", "kyo": "きょ", "gya": "ぎゃ", "gyu": "ぎゅ", "gyo": "ぎょ",
    "sha": "しゃ", "shu": "しゅ", "sho": "しょ", "sya": "しゃ", "syu": "しゅ", "syo": "しょ",
    "ja": "じゃ", "ju": "じゅ", "jo": "じょ", "jya": "じゃ", "jyu": "じゅ", "jyo": "じょ",
    "cha": "ちゃ", "chu": "ちゅ", "cho": "ちょ", "cya": "ちゃ", "cyu": "ちゅ", "cyo": "ちょ",
    "nya": "にゃ", "nyu": "にゅ", "nyo": "にょ", "hya": "ひゃ", "hyu": "ひゅ", "hyo": "ひょ",
    "bya": "びゃ", "byu": "びゅ", "byo": "びょ", "pya": "ぴゃ", "pyu": "ぴゅ", "pyo": "ぴょ",
    "mya": "みゃ", "myu": "みゅ", "myo": "みょ", "rya": "りゃ", "ryu": "りゅ", "ryo": "りょ",
    "tsu": "つ", "shi": "し", "chi": "ち", "fu": "ふ",
    "ka": "か", "ki": "き", "ku": "く", "ke": "け", "ko": "こ",
    "ga": "が", "gi": "ぎ", "gu": "ぐ", "ge": "げ", "go": "ご",
    "sa": "さ", "si": "し", "su": "す", "se": "せ", "so": "そ",
    "za": "ざ", "zi": "じ", "zu": "ず", "ze": "ぜ", "zo": "ぞ", "ji": "じ",
    "ta": "た", "ti": "ち", "tu": "つ", "te": "て", "to": "と",
    "da": "だ", "di": "ぢ", "du": "づ", "de": "で", "do": "ど",
    "na": "な", "ni": "に", "nu": "ぬ", "ne": "ね", "no": "の",
    "ha": "は", "hi": "ひ", "hu": "ふ", "he": "へ", "ho": "ほ",
    "ba": "ば", "bi": "び", "bu": "ぶ", "be": "べ", "bo": "ぼ",
    "pa": "ぱ", "pi": "ぴ", "pu": "ぷ", "pe": "ぺ", "po": "ぽ",
    "ma": "ま", "mi": "み", "mu": "む", "me": "め", "mo": "も",
    "ya": "や", "yu": "ゆ", "yo": "よ", "ra": "ら", "ri": "り", "ru": "る", "re": "れ", "ro": "ろ",
    "wa": "わ", "wi": "ゐ", "we": "ゑ", "wo": "を", "a": "あ", "i": "い", "u": "う", "e": "え", "o": "お", "n": "ん",
}


def korean_yale_to_hangul(value: str) -> str | None:
    s = value.upper()
    initial_key = ""
    for key in sorted((k for k in KOREAN_INITIALS if k), key=len, reverse=True):
        if s.startswith(key):
            initial_key = key
            break
    rest = s[len(initial_key):]
    vowel_key = None
    for key in sorted(KOREAN_VOWELS, key=len, reverse=True):
        if rest.startswith(key):
            vowel_key = key
            break
    if not vowel_key:
        return None
    final_key = rest[len(vowel_key):]
    if final_key not in KOREAN_FINALS:
        return None
    codepoint = 0xAC00 + (KOREAN_INITIALS[initial_key] * 21 + KOREAN_VOWELS[vowel_key]) * 28 + KOREAN_FINALS[final_key]
    return chr(codepoint)


def japanese_romaji_to_hiragana(value: str) -> str | None:
    s = value.lower()
    out = []
    i = 0
    while i < len(s):
        if i + 1 < len(s) and s[i] == s[i + 1] and s[i] not in "aeioun":
            out.append("っ")
            i += 1
            continue
        if s[i] == "n" and (i + 1 == len(s) or s[i + 1] not in "aeiouy"):
            out.append("ん")
            i += 1
            continue
        matched = None
        for size in (3, 2, 1):
            part = s[i:i + size]
            if part in JAPANESE_ROMAJI:
                matched = part
                break
        if not matched:
            return None
        out.append(JAPANESE_ROMAJI[matched])
        i += len(matched)
    return "".join(out)


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

        CREATE TABLE reading_preferences (
            char TEXT NOT NULL,
            system TEXT NOT NULL,
            reading TEXT NOT NULL,
            rank INTEGER NOT NULL,
            source TEXT NOT NULL,
            PRIMARY KEY (char, system, reading, source)
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
            for rank, reading in enumerate(fields.get("kKorean", "").split(), start=1):
                hangul = korean_yale_to_hangul(reading)
                if hangul:
                    conn.execute(
                        "INSERT OR IGNORE INTO character_readings VALUES (?, ?, ?, ?)",
                        (ch, "korean", hangul, "unihan"),
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO reading_preferences VALUES (?, ?, ?, ?, ?)",
                        (ch, "korean", hangul, rank, "unihan:kKorean"),
                    )
            for field in ("kJapaneseOn", "kJapaneseKun"):
                for reading in fields.get(field, "").split():
                    hiragana = japanese_romaji_to_hiragana(reading)
                    if hiragana:
                        conn.execute(
                            "INSERT OR IGNORE INTO character_readings VALUES (?, ?, ?, ?)",
                            (ch, "japanese", hiragana, f"unihan:{field}"),
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


def create_app_indexes(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS entry_index;
        DROP TABLE IF EXISTS entry_fts;

        CREATE TABLE entry_index (
            text TEXT PRIMARY KEY,
            traditional TEXT,
            simplified TEXT,
            has_character INTEGER NOT NULL DEFAULT 0,
            has_word INTEGER NOT NULL DEFAULT 0,
            primary_pinyin TEXT,
            primary_jyutping TEXT,
            english_summary TEXT,
            hsk_min_level INTEGER
        );

        CREATE VIRTUAL TABLE entry_fts USING fts5(
            text UNINDEXED,
            traditional,
            simplified,
            pinyin,
            jyutping,
            english
        );
        """
    )

    def upsert_entry(
        text_value: str,
        traditional: str | None = None,
        simplified: str | None = None,
        has_character: bool = False,
        has_word: bool = False,
        pinyin: str | None = None,
        jyutping: str | None = None,
        english: str | None = None,
        hsk_level: int | None = None,
    ) -> None:
        existing = conn.execute("SELECT * FROM entry_index WHERE text=?", (text_value,)).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE entry_index SET
                    traditional=COALESCE(traditional, ?),
                    simplified=COALESCE(simplified, ?),
                    has_character=max(has_character, ?),
                    has_word=max(has_word, ?),
                    primary_pinyin=COALESCE(primary_pinyin, ?),
                    primary_jyutping=COALESCE(primary_jyutping, ?),
                    english_summary=COALESCE(english_summary, ?),
                    hsk_min_level=CASE
                        WHEN hsk_min_level IS NULL THEN ?
                        WHEN ? IS NULL THEN hsk_min_level
                        ELSE min(hsk_min_level, ?)
                    END
                WHERE text=?
                """,
                (
                    traditional,
                    simplified,
                    1 if has_character else 0,
                    1 if has_word else 0,
                    pinyin,
                    jyutping,
                    english,
                    hsk_level,
                    hsk_level,
                    hsk_level,
                    text_value,
                ),
            )
        else:
            conn.execute(
                "INSERT INTO entry_index VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    text_value,
                    traditional,
                    simplified,
                    1 if has_character else 0,
                    1 if has_word else 0,
                    pinyin,
                    jyutping,
                    english,
                    hsk_level,
                ),
            )

    for row in conn.execute(
        """
        SELECT c.char, c.definition,
               (SELECT reading FROM character_readings r WHERE r.char=c.char AND r.system='pinyin' ORDER BY source LIMIT 1) AS pinyin,
               (SELECT reading FROM character_readings r WHERE r.char=c.char AND r.system='jyutping' ORDER BY source LIMIT 1) AS jyutping,
               (
                   SELECT min(level) FROM hsk_character_levels h
                   WHERE h.char=c.char OR h.char IN (
                       SELECT variant FROM character_variants v
                       WHERE v.char=c.char AND v.variant_type IN ('simplified', 'traditional')
                   )
               ) AS hsk_level
        FROM characters c
        """
    ):
        upsert_entry(row[0], has_character=True, pinyin=row[2], jyutping=row[3], english=row[1], hsk_level=row[4])

    for row in conn.execute("SELECT id, traditional, simplified, pinyin_diacritic, definitions_json FROM words"):
        definitions = json.loads(row[4]) if row[4] else []
        english = "; ".join(definitions[:3])
        hsk_row = conn.execute(
            "SELECT min(level) FROM hsk_words WHERE traditional=? OR simplified=? OR word=?",
            (row[1], row[2], row[2]),
        ).fetchone()
        hsk_level = hsk_row[0] if hsk_row else None
        upsert_entry(row[1], traditional=row[1], simplified=row[2], has_word=True, pinyin=row[3], english=english, hsk_level=hsk_level)
        upsert_entry(row[2], traditional=row[1], simplified=row[2], has_word=True, pinyin=row[3], english=english, hsk_level=hsk_level)

    conn.execute(
        """
        INSERT INTO entry_fts(text, traditional, simplified, pinyin, jyutping, english)
        SELECT text, COALESCE(traditional,''), COALESCE(simplified,''),
               COALESCE(primary_pinyin,''), COALESCE(primary_jyutping,''), COALESCE(english_summary,'')
        FROM entry_index
        """
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
        "reading_preferences",
        "entry_index",
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
        create_app_indexes(conn)
        conn.commit()
        print_summary(conn)
    finally:
        conn.close()
    write_manifest(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
