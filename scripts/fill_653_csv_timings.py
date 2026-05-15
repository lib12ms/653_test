"""[DEPRECATED] 모드 통합(2026-05-15) 이후 빠른/정밀 모드 구분이 사라졌습니다.
이 스크립트는 두 모드 비교를 위해 작성된 것으로, 현재는 사용하지 않습니다.

653_prompt_test_template.csv 의 ISBN 행마다 field653 API를 호출해 소요 시간을 기록합니다.

요구사항: 백엔드(uvicorn 등) 실행 + Aladin/OpenAI/NLK 키가 유효할 것.

  I2M_653_API_BASE  기본값 http://127.0.0.1:8000 (프로젝트 루트 `.env` 에 두면 자동 로드)

배포 API 예: https://six53-test.onrender.com

같은 ISBN·모드는 서버 TTL 캐시에 걸리면 재요청이 매우 빨라질 수 있습니다.
콜드 경로 시간을 재려면 `.env`에서 isbn_cache_ttl_s=0 으로 두고 서버를 재시작하세요.

예:
  python scripts/fill_653_csv_timings.py
  python scripts/fill_653_csv_timings.py --csv D:/653_test/653_prompt_test_template.csv
  python scripts/fill_653_csv_timings.py --init-only    # 빈 시간 컬럼만 추가(측정 없음)
  python scripts/fill_653_csv_timings.py --refresh-shorter-only  # 소요더짧은모드만 재계산
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    pass

TIMEOUT_S = 180
COL_FAST = "빠른모드_소요초"
COL_PRECISE = "정밀모드_소요초"
COL_SHORTER = "소요더짧은모드"
ANCHOR_COL = "정밀모드_653"
SHORTER_EPS_SEC = 0.001

_TIMING_COLS = (COL_FAST, COL_PRECISE, COL_SHORTER)


def _api_base() -> str:
    return os.environ.get("I2M_653_API_BASE", "http://127.0.0.1:8000").rstrip("/")


def ensure_timing_columns(fieldnames: list[str]) -> list[str]:
    """정밀모드_653 바로 다음에 소요 시간·판정 열 순서를 갖도록 헤더를 정규화."""
    fn = [f for f in fieldnames if f not in _TIMING_COLS]
    try:
        i = fn.index(ANCHOR_COL) + 1
    except ValueError as e:
        raise SystemExit(f"CSV에 '{ANCHOR_COL}' 열이 필요합니다.") from e
    return fn[:i] + [COL_FAST, COL_PRECISE, COL_SHORTER] + fn[i:]


def _parse_clean_seconds(cell: str) -> float | None:
    """순수 초 값만 허용(오류 메시지가 붙은 셀은 비교 불가로 처리)."""
    s = (cell or "").strip()
    if not s or "(" in s or "HTTP" in s or "URLError" in s:
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)", s.strip('"'))
    if not m:
        return None
    return float(m.group(1))


def compute_shorter_mode(fast_cell: str, precise_cell: str) -> str:
    """더 짧게 걸린 쪽 또는 동일·비교불가 반환."""
    f_sec = _parse_clean_seconds(fast_cell)
    p_sec = _parse_clean_seconds(precise_cell)
    if f_sec is None or p_sec is None:
        return "비교불가"
    if abs(f_sec - p_sec) < SHORTER_EPS_SEC:
        return "동일"
    return "빠른" if f_sec < p_sec else "정밀"


def refresh_shorter_mode_column(rows: list[dict[str, str]]) -> None:
    for d in rows:
        fv, pv = (d.get(COL_FAST) or "").strip(), (d.get(COL_PRECISE) or "").strip()
        if not fv and not pv:
            d[COL_SHORTER] = ""
        else:
            d[COL_SHORTER] = compute_shorter_mode(fv, pv)


def _timed_post(url: str, body: dict) -> tuple[float, bool, str]:
    """(경과 초, 성공 여부, 실패 시 메모)."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        elapsed = time.perf_counter() - t0
        try:
            err_body = e.read().decode("utf-8")[:400]
        except Exception:
            err_body = str(e)
        return elapsed, False, f"HTTP {e.code}: {err_body}"
    except Exception as e:
        elapsed = time.perf_counter() - t0
        return elapsed, False, repr(e)

    elapsed = time.perf_counter() - t0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return elapsed, False, "invalid JSON"
    ok = bool(payload.get("success"))
    return elapsed, ok, "" if ok else (payload.get("error") or "success=false")


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    sio = io.StringIO()
    w = csv.DictWriter(sio, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for d in rows:
        w.writerow({fn: d.get(fn, "") for fn in fieldnames})
    path.write_text("\ufeff" + sio.getvalue(), encoding="utf-8")


def run(csv_path: Path, init_only: bool, refresh_shorter_only: bool) -> None:
    raw = csv_path.read_text(encoding="utf-8-sig")
    reader = csv.DictReader(raw.splitlines())
    if not reader.fieldnames:
        raise SystemExit("헤더 없음")

    fieldnames = ensure_timing_columns(list(reader.fieldnames))
    rows_out: list[dict[str, str]] = []
    for r in reader:
        d: dict[str, str] = {}
        for k in fieldnames:
            v = ""
            if k in r and r[k] is not None:
                v = str(r[k]).strip()
            d[k] = v
        rows_out.append(d)

    if refresh_shorter_only:
        refresh_shorter_mode_column(rows_out)
        _write_csv(csv_path, fieldnames, rows_out)
        print(f"소요더짧은모드 재계산 후 저장: {csv_path}")
        return

    if init_only:
        for d in rows_out:
            d[COL_FAST] = ""
            d[COL_PRECISE] = ""
        refresh_shorter_mode_column(rows_out)
        _write_csv(csv_path, fieldnames, rows_out)
        print(f"시간 열을 추가했습니다(비어 있음): {csv_path}")
        return

    api = _api_base()
    endpoint = f"{api}/api/field653"
    print(f"측정 API: {endpoint}")
    any_fail = False
    for d in rows_out:
        isbn = (d.get("ISBN") or "").strip().replace("-", "").replace(" ", "")
        if not isbn:
            d[COL_FAST] = ""
            d[COL_PRECISE] = ""
            continue
        for mode, col in (("fast", COL_FAST), ("precise", COL_PRECISE)):
            elapsed, ok, note = _timed_post(endpoint, {"isbn": isbn, "analysis_mode": mode})
            if ok:
                d[col] = f"{elapsed:.3f}"
            else:
                any_fail = True
                suffix = f" ({note})" if note else ""
                d[col] = f"{elapsed:.3f}{suffix}" if elapsed > 0 else note or "0"
            print(f"  {isbn} {mode}: {d[col]}")

    refresh_shorter_mode_column(rows_out)
    _write_csv(csv_path, fieldnames, rows_out)
    print(f"저장: {csv_path}")
    if any_fail:
        print("일부 요청이 실패했습니다. 해당 셀에 오류 메시지가 포함됩니다.", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="653 CSV에 ISBN별 빠른/정밀 모드 소요 시간 기록")
    parser.add_argument(
        "--csv",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "653_prompt_test_template.csv",
        help="대상 CSV 경로",
    )
    parser.add_argument(
        "--init-only",
        action="store_true",
        help="시간 열만 추가하고 비워 둠(API 호출 없음)",
    )
    parser.add_argument(
        "--refresh-shorter-only",
        action="store_true",
        help="현재 두 소요초 열만 보고 소요더짧은모드 재계산(API 없음)",
    )
    args = parser.parse_args()
    if not args.csv.is_file():
        raise SystemExit(f"파일 없음: {args.csv}")
    run(
        args.csv,
        init_only=args.init_only,
        refresh_shorter_only=args.refresh_shorter_only,
    )


if __name__ == "__main__":
    main()
