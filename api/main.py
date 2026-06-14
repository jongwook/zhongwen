from __future__ import annotations

import json
import os
import re
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any

import jieba
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "zhongwen.sqlite"

app = FastAPI(title="Zhongwen API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

CJK_RE = re.compile(r"[\u3400-\u9fff\U00020000-\U000323af]")
UCP_RE = re.compile(r"^U\+([0-9a-fA-F]{4,6})$")

PINYIN_INITIALS = {
    "zh": "ㄓ", "ch": "ㄔ", "sh": "ㄕ",
    "b": "ㄅ", "p": "ㄆ", "m": "ㄇ", "f": "ㄈ", "d": "ㄉ", "t": "ㄊ", "n": "ㄋ", "l": "ㄌ",
    "g": "ㄍ", "k": "ㄎ", "h": "ㄏ", "j": "ㄐ", "q": "ㄑ", "x": "ㄒ", "r": "ㄖ",
    "z": "ㄗ", "c": "ㄘ", "s": "ㄙ",
}
PINYIN_FINALS = {
    "": "", "a": "ㄚ", "o": "ㄛ", "e": "ㄜ", "ê": "ㄝ", "ai": "ㄞ", "ei": "ㄟ", "ao": "ㄠ", "ou": "ㄡ",
    "an": "ㄢ", "en": "ㄣ", "ang": "ㄤ", "eng": "ㄥ", "er": "ㄦ",
    "i": "ㄧ", "ia": "ㄧㄚ", "ie": "ㄧㄝ", "iao": "ㄧㄠ", "iu": "ㄧㄡ", "ian": "ㄧㄢ", "in": "ㄧㄣ", "iang": "ㄧㄤ", "ing": "ㄧㄥ", "iong": "ㄩㄥ",
    "u": "ㄨ", "ua": "ㄨㄚ", "uo": "ㄨㄛ", "uai": "ㄨㄞ", "ui": "ㄨㄟ", "uei": "ㄨㄟ", "uan": "ㄨㄢ", "un": "ㄨㄣ", "uen": "ㄨㄣ", "uang": "ㄨㄤ", "ong": "ㄨㄥ",
    "ü": "ㄩ", "üe": "ㄩㄝ", "üan": "ㄩㄢ", "ün": "ㄩㄣ",
    "-i": "",  # apical vowel after zh/ch/sh/r/z/c/s
}
PINYIN_TONES = {"1": "", "2": "ˊ", "3": "ˇ", "4": "ˋ", "5": "˙"}
TONE_MARKS = {
    "ā": ("a", "1"), "á": ("a", "2"), "ǎ": ("a", "3"), "à": ("a", "4"),
    "ē": ("e", "1"), "é": ("e", "2"), "ě": ("e", "3"), "è": ("e", "4"),
    "ī": ("i", "1"), "í": ("i", "2"), "ǐ": ("i", "3"), "ì": ("i", "4"),
    "ō": ("o", "1"), "ó": ("o", "2"), "ǒ": ("o", "3"), "ò": ("o", "4"),
    "ū": ("u", "1"), "ú": ("u", "2"), "ǔ": ("u", "3"), "ù": ("u", "4"),
    "ǖ": ("ü", "1"), "ǘ": ("ü", "2"), "ǚ": ("ü", "3"), "ǜ": ("ü", "4"), "ü": ("ü", "5"),
}



class SegmentRequest(BaseModel):
    text: str


def db_path() -> Path:
    return Path(os.environ.get("ZHONGWEN_DB", DEFAULT_DB))


def connect() -> sqlite3.Connection:
    path = db_path()
    if not path.exists():
        raise HTTPException(500, f"Database not found: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def rows(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with connect() as conn:
        return [dict(row) for row in conn.execute(sql, params)]


def one(sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None


def parse_defs(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        data = json.loads(value)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def word_dict(row: dict[str, Any]) -> dict[str, Any]:
    row = dict(row)
    row["definitions"] = parse_defs(row.pop("definitions_json", None))
    for key in ("pinyin_diacritic", "primary_pinyin"):
        if row.get(key):
            row[key] = normalize_reading("pinyin", row[key])
    return row


def normalize_pinyin_syllable(syllable: str) -> tuple[str, str]:
    s = syllable.strip().lower().replace("u:", "ü").replace("v", "ü")
    tone = "5"
    if s and s[-1] in "12345":
        tone = s[-1]
        s = s[:-1]
    chars = []
    for ch in s:
        if ch in TONE_MARKS:
            base, marked_tone = TONE_MARKS[ch]
            chars.append(base)
            if marked_tone != "5":
                tone = marked_tone
        else:
            chars.append(ch)
    return "".join(chars), tone


def split_pinyin_initial_final(syllable: str) -> tuple[str, str]:
    initial = ""
    for candidate in ("zh", "ch", "sh"):
        if syllable.startswith(candidate):
            initial = candidate
            break
    if not initial and syllable[:1] in PINYIN_INITIALS:
        initial = syllable[:1]
    final = syllable[len(initial):]

    if initial in {"j", "q", "x"} and final.startswith("u"):
        final = "ü" + final[1:]
    elif syllable.startswith("yu"):
        initial = ""
        final = "ü" + syllable[2:]
    elif syllable == "yue":
        initial = ""
        final = "üe"
    elif syllable.startswith("y"):
        initial = ""
        rest = syllable[1:]
        if rest == "":
            final = "i"
        elif rest.startswith("i"):
            final = rest
        else:
            final = "i" + rest
    elif syllable.startswith("w"):
        initial = ""
        rest = syllable[1:]
        final = "u" + rest if rest else "u"

    if initial in {"zh", "ch", "sh", "r", "z", "c", "s"} and final == "i":
        final = "-i"
    return initial, final


def pinyin_syllable_to_zhuyin(syllable: str) -> str | None:
    normalized, tone = normalize_pinyin_syllable(syllable)
    if not normalized:
        return None
    initial, final = split_pinyin_initial_final(normalized)
    if initial not in PINYIN_INITIALS and initial != "":
        return None
    if final not in PINYIN_FINALS:
        return None
    base = PINYIN_INITIALS.get(initial, "") + PINYIN_FINALS[final]
    tone_mark = PINYIN_TONES.get(tone, "")
    return tone_mark + base if tone == "5" and tone_mark else base + tone_mark


def pinyin_to_zhuyin(reading: str) -> str | None:
    converted = []
    for syllable in reading.split():
        zhuyin = pinyin_syllable_to_zhuyin(syllable)
        if not zhuyin:
            return None
        converted.append(zhuyin)
    return " ".join(converted)


def with_zhuyin_readings(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = list(items)
    for item in items:
        if item.get("system") != "pinyin":
            continue
        zhuyin = pinyin_to_zhuyin(item.get("reading", ""))
        if zhuyin:
            out.append({"char": item.get("char"), "system": "zhuyin", "reading": zhuyin, "source": f"derived from {item.get('source', 'pinyin')}"})
    return unique_readings(out)


def normalize_reading(system: str, reading: str) -> str:
    if system == "pinyin":
        return reading.lower()
    return reading


def codepoint_id(text: str) -> str:
    return " ".join(f"U+{ord(ch):04X}" for ch in text)


def variant_display_items(subject: str) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows(
        """
        SELECT variant AS text FROM character_variants WHERE char=?
        UNION
        SELECT char AS text FROM character_variants WHERE variant=?
        """,
        (subject, subject),
    ):
        text_value = row["text"]
        if not text_value or text_value == subject:
            continue
        codepoint = codepoint_id(text_value)
        if codepoint in seen:
            continue
        seen.add(codepoint)
        out.append({"text": text_value, "codepoint": codepoint})
    return sorted(out, key=lambda item: item["codepoint"])


def unique_readings(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    order = {"pinyin": 0, "zhuyin": 1, "jyutping": 2, "korean": 3, "japanese": 4}
    normalized_items = []
    for index, item in enumerate(items):
        item = dict(item)
        item["reading"] = normalize_reading(item.get("system", ""), item.get("reading", ""))
        item["_index"] = index
        normalized_items.append(item)
    for item in sorted(normalized_items, key=lambda r: (order.get(r.get("system", ""), 9), r.get("_index", 0))):
        key = (item.get("system", ""), item.get("reading", ""))
        if key in seen:
            continue
        seen.add(key)
        item.pop("_index", None)
        out.append(item)
    return out


def canonical_chars_for(char: str) -> list[str]:
    candidates = [char]
    row = one("SELECT normalized_nfkc FROM characters WHERE char=?", (char,))
    if row and row.get("normalized_nfkc"):
        candidates.append(row["normalized_nfkc"])
    candidates.extend(
        item["variant"]
        for item in rows(
            """
            SELECT variant FROM character_variants
            WHERE char=? AND variant_type IN ('compatibility', 'opencc:CJK_Compatibility_Ideographs.txt')
            """,
            (char,),
        )
    )
    out = []
    seen = set()
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
    return out


def character_readings_for_char(char: str) -> list[dict[str, Any]]:
    reading_rows: list[dict[str, Any]] = []
    korean_preferred = korean_reading_char(char)
    for candidate in canonical_chars_for(char):
        inherited = candidate != char
        for row in rows(
            """
            SELECT cr.* FROM character_readings cr
            LEFT JOIN reading_preferences rp
              ON rp.char=cr.char AND rp.system=cr.system AND rp.reading=cr.reading
            WHERE cr.char=?
            ORDER BY cr.system, COALESCE(rp.rank, 999), cr.reading
            """,
            (candidate,),
        ):
            if row.get("system") == "korean" and korean_preferred != char:
                continue
            row["char"] = char
            if inherited:
                row["source"] = f"{row['source']} via {candidate}"
            reading_rows.append(row)
        for row in rows(
            """
            SELECT ? AS char, 'pinyin' AS system, pinyin_diacritic AS reading, source
            FROM words
            WHERE (traditional=? OR simplified=?)
              AND length(traditional)=1
              AND length(simplified)=1
              AND pinyin_diacritic IS NOT NULL
            """,
            (char, candidate, candidate),
        ):
            if inherited:
                row["source"] = f"{row['source']} via {candidate}"
            reading_rows.append(row)
    if korean_preferred != char:
        for row in rows(
            """
            SELECT cr.* FROM character_readings cr
            LEFT JOIN reading_preferences rp
              ON rp.char=cr.char AND rp.system=cr.system AND rp.reading=cr.reading
            WHERE cr.char=? AND cr.system='korean'
            ORDER BY COALESCE(rp.rank, 999), cr.reading
            """,
            (korean_preferred,),
        ):
            row["char"] = char
            row["source"] = f"{row['source']} via {korean_preferred}"
            reading_rows.append(row)
    return unique_readings(reading_rows)


def korean_reading_char(char: str) -> str:
    variants = rows(
        """
        SELECT variant FROM character_variants
        WHERE char=? AND variant_type IN ('traditional', 'opencc:STCharacters.txt')
        ORDER BY CASE WHEN variant=? THEN 1 ELSE 0 END, variant
        """,
        (char, char),
    )
    for row in variants:
        if row["variant"] != char:
            return row["variant"]
    return char


def preferred_korean_reading(char: str) -> str:
    row = one(
        """
        SELECT reading FROM reading_preferences
        WHERE char=? AND system='korean'
        ORDER BY rank LIMIT 1
        """,
        (char,),
    )
    return row["reading"] if row else ""


def preferred_character_reading(char: str, system: str) -> str:
    reading_char = korean_reading_char(char) if system == "korean" else char
    if system == "korean":
        preferred = preferred_korean_reading(reading_char)
        if preferred:
            return preferred
    candidates = [item for item in character_readings_for_char(reading_char) if item["system"] == system]
    if not candidates:
        return ""
    if system == "japanese":
        on = [item for item in candidates if "kJapaneseOn" in item.get("source", "")]
        if on:
            return on[0]["reading"]
    return candidates[0]["reading"]


def word_readings_for_words(word_rows: list[dict[str, Any]], text_value: str) -> list[dict[str, Any]]:
    if not word_rows:
        return []
    direct: list[dict[str, Any]] = []
    for word in word_rows:
        if word.get("pinyin_diacritic"):
            direct.append({"char": None, "system": "pinyin", "reading": word["pinyin_diacritic"], "source": word.get("source", "words")})
    ids = [w["id"] for w in word_rows]
    placeholders = ",".join("?" for _ in ids)
    direct.extend(
        rows(
            f"""
            SELECT NULL AS char, system, reading, source
            FROM word_readings
            WHERE word_id IN ({placeholders}) AND system != 'pinyin'
            ORDER BY word_id, system, source
            """,
            tuple(ids),
        )
    )
    direct = unique_readings(direct)
    systems = {item["system"] for item in direct}
    fallback = []
    char_summaries = [entry_summary(ch) for ch in text_value]
    if "pinyin" not in systems:
        pinyin = " ".join(item.get("primary_pinyin") or "" for item in char_summaries).strip()
        if pinyin:
            fallback.append({"char": None, "system": "pinyin", "reading": pinyin, "source": "per_character"})
    if "jyutping" not in systems:
        jyutping = " ".join(item.get("primary_jyutping") or "" for item in char_summaries).strip()
        if jyutping:
            fallback.append({"char": None, "system": "jyutping", "reading": jyutping, "source": "per_character"})
    for system in ("korean", "japanese"):
        if system in systems:
            continue
        pieces = [preferred_character_reading(char, system) for char in text_value]
        joined = "".join(pieces).strip()
        if joined:
            fallback.append({"char": None, "system": system, "reading": joined, "source": "per_character"})
    return unique_readings(direct + fallback)


def hsk_for_text(text: str) -> dict[str, Any]:
    char_candidates = [text]
    if len(text) == 1:
        char_candidates.extend(
            row["variant"]
            for row in rows(
                "SELECT variant FROM character_variants WHERE char=? AND variant_type IN ('simplified', 'traditional')",
                (text,),
            )
        )
    placeholders = ",".join("?" for _ in char_candidates)
    return {
        "word_levels": rows(
            """
            SELECT word, traditional, simplified, level, hsk_version, source
            FROM hsk_words
            WHERE word=? OR traditional=? OR simplified=?
            ORDER BY level, word
            """,
            (text, text, text),
        ),
        "character_levels": rows(
            f"""
            SELECT char, level, hsk_version, source
            FROM hsk_character_levels
            WHERE char IN ({placeholders})
            ORDER BY level, char
            """,
            tuple(char_candidates),
        ),
    }


def entry_summary(text: str) -> dict[str, Any]:
    exact = one("SELECT * FROM entry_index WHERE text=?", (text,))
    if exact:
        if exact.get("primary_pinyin"):
            exact["primary_pinyin"] = normalize_reading("pinyin", exact["primary_pinyin"])
        if exact.get("has_character") and (not exact.get("primary_pinyin") or not exact.get("primary_jyutping")):
            readings = character_readings_for_char(text)
            exact["primary_pinyin"] = exact.get("primary_pinyin") or next((item["reading"] for item in readings if item["system"] == "pinyin"), None)
            exact["primary_jyutping"] = exact.get("primary_jyutping") or next((item["reading"] for item in readings if item["system"] == "jyutping"), None)
        return exact
    ch = one("SELECT char AS text, definition AS english_summary FROM characters WHERE char=?", (text,))
    if ch:
        readings = character_readings_for_char(text)
        pinyin = next((item["reading"] for item in readings if item["system"] == "pinyin"), None)
        jyutping = next((item["reading"] for item in readings if item["system"] == "jyutping"), None)
        ch["primary_pinyin"] = pinyin
        ch["primary_jyutping"] = jyutping
        ch.update({"has_character": 1, "has_word": 0})
        return ch
    word = one(
        """
        SELECT COALESCE(simplified, traditional) AS text, pinyin_diacritic AS primary_pinyin,
               definitions_json AS definitions_json
        FROM words WHERE traditional=? OR simplified=? LIMIT 1
        """,
        (text, text),
    )
    if word:
        defs = parse_defs(word.pop("definitions_json"))
        if word.get("primary_pinyin"):
            word["primary_pinyin"] = normalize_reading("pinyin", word["primary_pinyin"])
        word.update({"has_character": 0, "has_word": 1, "english_summary": "; ".join(defs[:3])})
        return word
    if exact and exact.get("primary_pinyin"):
        exact["primary_pinyin"] = normalize_reading("pinyin", exact["primary_pinyin"])
    return {"text": text, "has_character": 0, "has_word": 0}


@lru_cache(maxsize=1)
def load_jieba() -> int:
    count = 0
    with connect() as conn:
        for row in conn.execute("SELECT term, frequency, tag FROM segmenter_terms"):
            jieba.add_word(row["term"], freq=int(row["frequency"] or 1), tag=row["tag"] or None)
            count += 1
    return count


@app.on_event("startup")
def startup() -> None:
    load_jieba()


@app.get("/api/health")
def health() -> dict[str, Any]:
    source_count = one("SELECT count(*) AS count FROM sources") or {"count": 0}
    return {"ok": True, "db": str(db_path()), "sources": source_count["count"], "jieba_terms": load_jieba()}


@app.get("/api/search")
def search(q: str = Query(..., min_length=1), limit: int = Query(30, ge=1, le=100)) -> dict[str, Any]:
    query = q.strip()
    if not query:
        return {"query": q, "results": []}
    cp = UCP_RE.match(query)
    if cp:
        ch = chr(int(cp.group(1), 16))
        return {"query": q, "results": [entry_summary(ch)]}

    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(items: list[dict[str, Any]], match_type: str) -> None:
        for item in items:
            text = item.get("text") or item.get("char") or item.get("traditional") or item.get("simplified")
            if not text or text in seen:
                continue
            item = dict(item)
            if item.get("primary_pinyin"):
                item["primary_pinyin"] = normalize_reading("pinyin", item["primary_pinyin"])
            item["match_type"] = match_type
            out.append(item)
            seen.add(text)
            if len(out) >= limit:
                return

    add(rows("SELECT * FROM entry_index WHERE text=? LIMIT ?", (query, limit)), "exact")
    add(rows("SELECT * FROM entry_index WHERE simplified=? OR traditional=? LIMIT ?", (query, query, limit)), "form")
    add(rows("SELECT * FROM entry_index WHERE primary_pinyin=? OR primary_jyutping=? LIMIT ?", (query, query, limit)), "reading")
    if len(out) < limit:
        add(
            rows(
                """
                SELECT ei.* FROM entry_fts f
                JOIN entry_index ei ON ei.text = f.text
                WHERE entry_fts MATCH ?
                ORDER BY ei.has_word DESC, ei.hsk_min_level IS NULL, ei.hsk_min_level, length(ei.text), bm25(entry_fts)
                LIMIT ?
                """,
                (query.replace('"', ' '), limit - len(out)),
            ),
            "fulltext",
        )
    if len(out) < limit and CJK_RE.search(query):
        add(rows("SELECT * FROM entry_index WHERE text LIKE ? LIMIT ?", (f"%{query}%", limit - len(out))), "contains")
    return {"query": q, "results": out[:limit]}


@app.get("/api/char/{char}")
def char_detail(char: str) -> dict[str, Any]:
    if len(char) != 1:
        raise HTTPException(400, "Character endpoint expects exactly one Unicode character")
    character = one("SELECT * FROM characters WHERE char=?", (char,))
    if not character:
        raise HTTPException(404, "Character not found")
    return {
        "character": character,
        "readings": with_zhuyin_readings(character_readings_for_char(char)),
        "variants": rows("SELECT * FROM character_variants WHERE char=? OR variant=? ORDER BY char, variant_type, source, variant", (char, char)),
        "variant_display": variant_display_items(char),
        "character_sources": rows("SELECT * FROM character_sources WHERE char=? ORDER BY source_field, value", (char,)),
        "hsk": hsk_for_text(char),
    }


@app.get("/api/word/{text}")
def word_detail(text: str) -> dict[str, Any]:
    word_rows = [word_dict(r) for r in rows("SELECT * FROM words WHERE traditional=? OR simplified=? ORDER BY id", (text, text))]
    if not word_rows:
        raise HTTPException(404, "Word not found")
    readings = with_zhuyin_readings(word_readings_for_words(word_rows, text))
    chars = [entry_summary(ch) for ch in text]
    return {"text": text, "words": word_rows, "readings": readings, "characters": chars, "hsk": hsk_for_text(text)}


@app.get("/api/entry/{text}")
def entry(text: str) -> dict[str, Any]:
    character = None
    char_payload = None
    if len(text) == 1:
        character = one("SELECT * FROM characters WHERE char=?", (text,))
        if character:
            char_payload = char_detail(text)
    word_rows = [word_dict(r) for r in rows("SELECT * FROM words WHERE traditional=? OR simplified=? ORDER BY id", (text, text))]
    word_readings = with_zhuyin_readings(word_readings_for_words(word_rows, text)) if word_rows else []
    related = rows(
        """
        SELECT id, traditional, simplified, pinyin_diacritic, definitions_json, source
        FROM words
        WHERE traditional LIKE ? OR simplified LIKE ?
        ORDER BY length(traditional), traditional
        LIMIT 80
        """,
        (f"%{text}%", f"%{text}%"),
    )
    return {
        "text": text,
        "summary": entry_summary(text),
        "character": character,
        "character_detail": char_payload,
        "words": word_rows,
        "word_readings": word_readings,
        "readings": char_payload["readings"] if char_payload else word_readings,
        "characters": [entry_summary(ch) for ch in text] if len(text) > 1 else [],
        "related_words": [word_dict(r) for r in related],
        "hsk": hsk_for_text(text),
    }


@app.post("/api/segment")
def segment(req: SegmentRequest) -> dict[str, Any]:
    load_jieba()
    tokens = []
    cursor = 0
    for token in jieba.cut(req.text, cut_all=False):
        start = req.text.find(token, cursor)
        if start < 0:
            start = cursor
        end = start + len(token)
        cursor = end
        summary = entry_summary(token)
        if len(token) > 1 and CJK_RE.search(token) and not summary.get("has_character") and not summary.get("has_word"):
            for offset, char in enumerate(token):
                tokens.append({"text": char, "start": start + offset, "end": start + offset + 1, "entry": entry_summary(char)})
        else:
            tokens.append({"text": token, "start": start, "end": end, "entry": summary})
    return {"text": req.text, "tokens": tokens}


@app.get("/api/hsk")
def hsk_overview() -> dict[str, Any]:
    return {
        "word_counts": rows("SELECT level, count(*) AS count FROM hsk_words GROUP BY level ORDER BY level"),
        "character_counts": rows("SELECT level, count(*) AS count FROM hsk_character_levels GROUP BY level ORDER BY level"),
    }


@app.get("/api/hsk/{level}")
def hsk_level(level: int) -> dict[str, Any]:
    if level < 1 or level > 6:
        raise HTTPException(400, "HSK level must be 1-6")
    return {
        "level": level,
        "words": rows("SELECT * FROM hsk_words WHERE level=? ORDER BY word", (level,)),
        "characters": rows("SELECT * FROM hsk_character_levels WHERE level=? ORDER BY char", (level,)),
    }


@app.get("/api/readings/{system}/{reading}")
def readings(system: str, reading: str) -> dict[str, Any]:
    if system not in {"pinyin", "jyutping", "korean", "japanese"}:
        raise HTTPException(400, "system must be pinyin, jyutping, korean, or japanese")
    chars = rows("SELECT * FROM character_readings WHERE system=? AND reading=? ORDER BY char", (system, reading))
    word_rs = rows(
        """
        SELECT w.id, w.traditional, w.simplified, w.pinyin_diacritic, w.definitions_json, wr.reading, wr.source
        FROM word_readings wr JOIN words w ON w.id=wr.word_id
        WHERE wr.system=? AND wr.reading=?
        ORDER BY length(w.traditional), w.traditional
        LIMIT 200
        """,
        (system, reading),
    )
    return {"system": system, "reading": reading, "characters": chars, "words": [word_dict(r) for r in word_rs]}


@app.get("/api/variants/{text}")
def variants(text: str) -> dict[str, Any]:
    return {
        "text": text,
        "character_variants": rows(
            "SELECT * FROM character_variants WHERE char=? OR variant=? ORDER BY char, variant_type, source, variant",
            (text, text),
        ),
        "conversion_mappings": rows(
            """
            SELECT * FROM conversion_mappings
            WHERE source_text=? OR target_text=? OR source_text LIKE ? OR target_text LIKE ?
            ORDER BY length(source_text), source_text, dictionary, target_text
            LIMIT 300
            """,
            (text, text, f"%{text}%", f"%{text}%"),
        ),
    }


@app.get("/api/sources")
def sources() -> dict[str, Any]:
    return {"sources": rows("SELECT * FROM sources ORDER BY key")}
