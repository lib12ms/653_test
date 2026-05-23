"""여행·전집 CategoryId 탐색."""
import os, json, urllib.request, time
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
key = os.environ.get("ALADIN_TTB_KEY", "")

test_ids = (
    list(range(1, 200))               # 낮은 번호대 전체
    + list(range(1300, 1600))         # 여행 후보
    + list(range(1900, 2100))         # 전집/세트 후보
    + list(range(47000, 49000, 10))   # 여행 후보 (기존 범위 촘촘히)
    + list(range(70000, 75000, 20))   # 전집/세트 후보
)

results = []
keywords = ["여행", "전집", "세트"]

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
                print(f"HIT  cid={cid}  {cat[:80]}")
    except Exception:
        pass
    time.sleep(0.05)

out = Path(__file__).resolve().parents[1] / "tmp_cids_travel.json"
out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\nhits: {len(results)}")
