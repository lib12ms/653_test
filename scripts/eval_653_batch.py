"""샘플 ISBN 배치 평가 스크립트.

사용 예:
python scripts/eval_653_batch.py 9791185676708 9791190292108
"""
from __future__ import annotations

import json
import sys
import urllib.request


API = "http://127.0.0.1:8000/api/field653"


def call(isbn: str) -> dict:
    body = json.dumps({"isbn": isbn}).encode("utf-8")
    req = urllib.request.Request(API, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main(argv: list[str]) -> None:
    isbns = argv[1:] or [
        "9791185676708",
        "9791190292108",
        "9788936434267",
    ]
    print("isbn,success,kw_count,keywords,category_clean")
    for isbn in isbns:
        try:
            data = call(isbn)
            kws = data.get("keywords") or []
            dbg = data.get("preprocess_debug") or {}
            cat = dbg.get("category_clean") or (data.get("aladin") or {}).get("category", "")
            print(f"{isbn},{data.get('success')},{len(kws)},\"{'|'.join(kws)}\",\"{cat}\"")
        except Exception as e:
            print(f"{isbn},False,0,\"\",\"ERROR:{e}\"")


if __name__ == "__main__":
    main(sys.argv)
