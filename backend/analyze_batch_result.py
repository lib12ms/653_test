"""500건 배치 결과 분석 스크립트
=============================
후처리 근거표 및 결과 부족 대응 분석에 필요한 수치를 산출합니다.

실행:
    cd backend
    python analyze_batch_result.py 653_신간500_YYYYMMDD_HHMMSS.csv
"""
from __future__ import annotations

import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path


# ── 1. CSV 로드 ───────────────────────────────────────────────────────────────

def load_csv(path: str) -> list[dict]:
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


# ── 2. 수치 파싱 헬퍼 ────────────────────────────────────────────────────────

def _int(v) -> int:
    try:
        return int(v)
    except (ValueError, TypeError):
        return 0

def _float(v) -> float:
    try:
        return float(v)
    except (ValueError, TypeError):
        return 0.0


# ── 3. 결과 부족 케이스 분류 ─────────────────────────────────────────────────

def classify_row(row: dict) -> str:
    """각 행을 5가지 케이스 중 하나로 분류."""
    flags = row.get("경고플래그", "")
    ai_cnt = _int(row.get("AI생성수"))
    final_cnt = _int(row.get("최종수"))
    blocked = _int(row.get("차단수"))

    if row.get("오류"):
        return "오류"
    if "텍스트fallback사용" in flags:
        return "텍스트fallback"
    if "카테고리fallback사용" in flags:
        return "카테고리fallback"
    if final_cnt < 5 and final_cnt > 0:
        return "부족(5개미만)"
    if final_cnt == 0:
        return "키워드없음"
    if ai_cnt > 0 and blocked / ai_cnt > 0.5:
        return "과다차단"
    return "정상"


# ── 4. 분석 함수들 ───────────────────────────────────────────────────────────

def section(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print('='*60)


def analyze(rows: list[dict]):
    valid = [r for r in rows if not r.get("오류")]
    total = len(rows)
    ok = len(valid)

    # ── 기본 통계 ──────────────────────────────────────────────────────────
    section("1. 기본 통계")
    print(f"  전체 처리: {total}권  |  성공: {ok}권  |  오류: {total - ok}권")

    scores = [_float(r.get("품질점수")) for r in valid]
    avg_score = sum(scores) / len(scores) if scores else 0
    print(f"  평균 품질점수: {avg_score:.3f}")

    ai_cnts  = [_int(r.get("AI생성수")) for r in valid]
    blk_cnts = [_int(r.get("차단수")) for r in valid]
    fin_cnts = [_int(r.get("최종수")) for r in valid]

    print(f"  AI생성수   평균: {sum(ai_cnts)/ok:.2f}  최소: {min(ai_cnts)}  최대: {max(ai_cnts)}")
    print(f"  차단수     평균: {sum(blk_cnts)/ok:.2f}  최소: {min(blk_cnts)}  최대: {max(blk_cnts)}")
    print(f"  최종수     평균: {sum(fin_cnts)/ok:.2f}  최소: {min(fin_cnts)}  최대: {max(fin_cnts)}")

    # 전체 차단율
    total_ai  = sum(ai_cnts)
    total_blk = sum(blk_cnts)
    overall_filter_rate = total_blk / total_ai if total_ai else 0
    print(f"  전체 차단율: {overall_filter_rate:.1%}  ({total_blk}/{total_ai})")

    # ── 경고 플래그 분포 ───────────────────────────────────────────────────
    section("2. 경고 플래그 분포")
    flag_counter: Counter = Counter()
    for r in valid:
        for flag in (r.get("경고플래그") or "").split("|"):
            f = flag.strip()
            if f:
                flag_counter[f] += 1
    for flag, cnt in flag_counter.most_common():
        print(f"  {flag:<25} {cnt:>4}건  ({cnt/ok:.1%})")

    # ── 결과 부족 케이스 분류 ─────────────────────────────────────────────
    section("3. 결과 부족 케이스 분류 (5가지)")
    case_counter: Counter = Counter()
    case_rows: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        case = classify_row(r)
        case_counter[case] += 1
        case_rows[case].append(r)

    label_map = {
        "정상":          "① 정상 (AI생성 충분, 차단율 낮음)",
        "과다차단":      "② 과다차단 (AI생성 있으나 차단율 50% 초과)",
        "텍스트fallback": "③ 텍스트 fallback (AI 유효키워드 0 → 텍스트 추출)",
        "카테고리fallback": "④ 카테고리 fallback (5개 미달 → 카테고리 보충)",
        "부족(5개미만)": "⑤ 보충 후에도 5개 미만",
        "키워드없음":    "⑥ 키워드 0개",
        "오류":          "⑦ 오류",
    }
    for case, label in label_map.items():
        cnt = case_counter.get(case, 0)
        print(f"  {label:<45} {cnt:>4}건  ({cnt/total:.1%})")

    # ── 분야별 품질점수 ────────────────────────────────────────────────────
    section("4. 분야별 품질점수 평균")
    group_scores: dict[str, list[float]] = defaultdict(list)
    for r in valid:
        g = r.get("분야그룹") or "기타"
        group_scores[g].append(_float(r.get("품질점수")))
    for group, sc in sorted(group_scores.items(), key=lambda x: -sum(x[1])/len(x[1])):
        avg = sum(sc) / len(sc)
        print(f"  {group:<12} {avg:.3f}  ({len(sc)}권)")

    # ── 검토 필요 현황 ─────────────────────────────────────────────────────
    section("5. 검토 필요(★) 현황")
    review_rows = [r for r in valid if r.get("검토필요") == "Y"]
    print(f"  검토 필요: {len(review_rows)}건 / {ok}건 ({len(review_rows)/ok:.1%})")

    # ── 대표 사례 출력 ─────────────────────────────────────────────────────
    section("6. 대표 사례 (각 케이스별 최대 3건)")
    for case in ["과다차단", "텍스트fallback", "카테고리fallback", "부족(5개미만)"]:
        samples = case_rows.get(case, [])[:3]
        if not samples:
            continue
        print(f"\n  ▶ {case}")
        for r in samples:
            print(f"    [{r.get('순번','?'):>3}] {r.get('제목','')[:30]:<30}  "
                  f"분야:{r.get('분야그룹',''):<8}  "
                  f"AI:{r.get('AI생성수','?')} 차단:{r.get('차단수','?')} 최종:{r.get('최종수','?')}  "
                  f"Q={r.get('품질점수','?')}  "
                  f"플래그:{r.get('경고플래그','')}")
            print(f"         키워드: {r.get('키워드목록','')[:70]}")

    # ── 후처리 규칙 적용 수치 요약 ────────────────────────────────────────
    section("7. 후처리 규칙 적용 수치 (논문 근거표용)")
    total_ok = ok
    backup_used   = flag_counter.get("텍스트fallback사용", 0)
    cat_used      = flag_counter.get("카테고리fallback사용", 0)
    over_blocked  = flag_counter.get("과다차단", 0)
    kw_short      = flag_counter.get("키워드부족", 0)
    ai_low        = flag_counter.get("AI생성부족", 0)

    print(f"  제목·저자 차단 적용 대상: 전 {total_ok}건 (항상 적용)")
    print(f"  저효용어 차단 후 평균 차단율: {overall_filter_rate:.1%}")
    print(f"  과다차단 발생(차단율>50%%): {over_blocked}건 ({over_blocked/total_ok:.1%})")
    print(f"  AI생성부족(3개미만 생성): {ai_low}건 ({ai_low/total_ok:.1%})")
    print(f"  텍스트 fallback 사용:     {backup_used}건 ({backup_used/total_ok:.1%})")
    print(f"  카테고리 fallback 사용:   {cat_used}건 ({cat_used/total_ok:.1%})")
    print(f"  보충 후에도 5개 미만:     {kw_short}건 ({kw_short/total_ok:.1%})")

    # ── AI 관련어 제한 적용 현황 (IT 분야) ──────────────────────────────
    section("8. 분야별 검토필요 비율")
    review_by_group: dict[str, list] = defaultdict(list)
    for r in valid:
        g = r.get("분야그룹") or "기타"
        review_by_group[g].append(r.get("검토필요") == "Y")
    for group, flags in sorted(review_by_group.items()):
        need = sum(flags)
        total_g = len(flags)
        print(f"  {group:<12} 검토필요: {need:>3}건 / {total_g:>3}건 ({need/total_g:.1%})")


# ── 5. 진입점 ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        # 가장 최신 신간500 CSV 자동 탐색
        base = Path(__file__).parent
        candidates = sorted(base.glob("653_신간500*.csv"), reverse=True)
        if not candidates:
            candidates = sorted(base.glob("653_신간*.csv"), reverse=True)
        if not candidates:
            print("Usage: python analyze_batch_result.py <CSV파일>")
            sys.exit(1)
        csv_path = str(candidates[0])
        print(f"자동 선택: {csv_path}")
    else:
        csv_path = sys.argv[1]

    rows = load_csv(csv_path)
    print(f"\n로드 완료: {csv_path}  ({len(rows)}행)")
    analyze(rows)
