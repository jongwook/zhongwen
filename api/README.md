# Zhongwen API

Run locally:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn api.main:app --reload --host 127.0.0.1 --port 8000
```

Set `ZHONGWEN_DB=/path/to/zhongwen.sqlite` to use a different database.
