"""여행 parent CID + 전집 CID 정밀 탐색."""
import os, json, urllib.request, time
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
key = os.environ.get("ALADIN_TTB_KEY", "")

# 여행 sub-CID가 160, 1912로 확인 → 그 사이 구간 + 전집 후보 구간 정밀 탐색
test_ids = (
    list(range(100, 165))          # 160 근처 앞
    + list(range(165, 300))        # 160 근처 뒤
    + list(range(1300, 1920))      # 1912 앞 구간
    + list(range(2000, 2500))      # 전집/세트 후보
    + list(range(3300, 3600))      # 전집 후보
    + list(range(55700, 57000, 5)) # 전집 후보
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
                print(f"HIT  cid={cid:6d}  {cat[:80]}")
    except Exception:
        pass
    time.sleep(0.04)

out = Path(__file__).resolve().parents[1] / "tmp_cids_travel2.json"
out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\nhits: {len(results)}")
