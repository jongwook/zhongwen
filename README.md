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

## Web App

Install dependencies:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cd web
npm install
```

Run the API from the repository root:

```sh
. .venv/bin/activate
uvicorn api.main:app --reload --host 127.0.0.1 --port 8000
```

Run the website from `web/`:

```sh
npm run dev
```

Open `http://127.0.0.1:5173`. The frontend expects the API at
`http://127.0.0.1:8000` unless `VITE_API_BASE` is set.

The API uses `ZHONGWEN_DB` when set; otherwise it reads
`data/zhongwen.sqlite`.

## Static Export

To build a complete static site for GitHub Pages or any static server, run from
the repository root with the project virtualenv active:

```sh
.venv/bin/python scripts/export_static.py --db data/zhongwen.sqlite --out static
```

This writes the Vite app and JSON data shards under `static/`. The static build
uses browser-side max-match segmentation and local data files instead of the
FastAPI API. For quick smoke tests, use a limited data export:

```sh
.venv/bin/python scripts/export_static.py --limit 200
```

Serve the result with any static server, for example:

```sh
python3 -m http.server 8080 --directory static
```

### Verification

```sh
.venv/bin/python -m py_compile api/main.py scripts/build_zhongwen_db.py scripts/export_static.py
cd web && npm run build
```

On Node 18, this project pins Vite 5 and overrides esbuild so the build works
locally. `npm audit` still reports one Vite dev-server advisory whose non-forced
fix currently requires a newer Vite release and Node 20+.
