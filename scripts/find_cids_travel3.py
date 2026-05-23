"""여행 parent CID + 전집 CID — 미탐색 구간 집중 탐색."""
import os, json, urllib.request, time
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
key = os.environ.get("ALADIN_TTB_KEY", "")

# 미탐색 구간: 1~99, 300~1300, 2500~3300, 3600~55700(step20)
test_ids = (
    list(range(1, 100))
    + list(range(300, 1300))
    + list(range(2500, 3300))
    + list(range(3600, 4500))
    + list(range(55700, 57000, 5))
    + list(range(57000, 60000, 20))
)

results = []
keywords = ["여행", "전집"]

for cid in test_ids:
    url = (
        f"https://www.aladin.co.kr/ttb/api/ItemList.aspx"
        f"?ttbkey={key}&QueryType=ItemNewAll&CategoryId={cid}"
        f"&MaxResults=1&SearchTarget=Book&output=js&Version=20131101"
    )
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8"))
        items = data.get("item", [])
        if items:
            cat = items[0].get("categoryName", "")
            if any(k in cat for k in keywords):
                results.append({"cid": cid, "full": cat[:80]})
                print(f"HIT  cid={cid:6d}  {cat[:80]}")
    except Exception:
        pass
    time.sleep(0.04)

out = Path(__file__).resolve().parents[1] / "tmp_cids_travel3.json"
out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\nhits: {len(results)}")
