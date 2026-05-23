"""알라딘 CategoryId 탐색 — 미확인 분야 parent CID 찾기."""
import os, json, urllib.request, time
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
key = os.environ.get("ALADIN_TTB_KEY", "")

test_ids = (
    list(range(1800, 1960, 3))     # 역사/좋은부모 상위 후보
    + list(range(50, 175))         # 경제경영 낮은쪽
    + list(range(52000, 53600, 50))  # 건강/취미 parent 후보
    + list(range(47000, 48600, 50))  # 여행 후보
    + list(range(54500, 55700, 50))  # 취미/요리 후보
)

results = []
keywords = ["역사", "경제경영", "건강", "취미", "요리", "살림", "여행", "좋은부모", "전집", "외국어"]

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
                results.append({"cid": cid, "full": cat[:70]})
    except Exception:
        pass
    time.sleep(0.03)

out = Path(__file__).resolve().parents[1] / "tmp_parent_final.json"
out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"hits: {len(results)}")
print(json.dumps(results, ensure_ascii=False, indent=2))
