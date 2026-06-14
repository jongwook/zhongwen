# Zhongwen Character Database

This repository builds a reproducible SQLite database for Chinese character
learning data.

## Build

```sh
python3 scripts/build_zhongwen_db.py
```

The builder downloads source files into `data/sources/` and writes:

- `data/zhongwen.sqlite`
- `data/source_manifest.json`

The SQLite database keeps source provenance in separate tables instead of
collapsing ambiguous character variants into a single mapping.

## Data Sources

- Unicode Unihan: character inventory, readings, definitions, variant metadata.
- CC-CEDICT: Chinese-English dictionary with traditional/simplified forms and
  numbered pinyin.
- OpenCC: phrase and character conversion dictionaries for Simplified,
  Traditional, Taiwan, Hong Kong, and Japanese Shinjitai variants.
- rime-cantonese: Jyutping dictionaries.
- HSK 1-6: optional overlay source configured in the build script. The schema
  stores `hsk_version` so older 1-6 data and newer 9-level data can coexist.

## Important Modeling Notes

Simplified/traditional conversion and regional normalization are context
dependent. Use OpenCC-style phrase dictionaries for conversion behavior and
Unihan variant fields for per-character metadata.

Unicode normalization is separate from Chinese regional variant policy. The
builder stores both Unicode compatibility variants and OpenCC mappings so an
application can choose its own display policy.
