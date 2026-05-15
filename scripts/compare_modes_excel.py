"""[DEPRECATED] 모드 통합(2026-05-15) 이후 빠른/정밀 모드 구분이 사라졌습니다.
이 스크립트는 두 모드 비교를 위해 작성된 것으로, 현재는 사용하지 않습니다.

ISBN 목록으로 빠른/정밀 모드를 비교하고 엑셀(.xlsx)로 저장합니다.

측정 항목: API 전체 소요 시간(알라딘+NLK+OpenAI), OpenAI 토큰(prompt/completion/total), 653 결과

요구: 백엔드 실행 중 (uvicorn), `.env`에 API 키 설정

  I2M_653_API_BASE  기본 http://127.0.0.1:8000

캐시로 시간이 왜곡되면 `.env`에 isbn_cache_ttl_s=0 후 서버 재시작.

예:
  python scripts/compare_modes_excel.py 9791168224506 9791194630456
  python scripts/compare_modes_excel.py --isbn-file isbns.txt -o 비교결과.xlsx
"""
from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    pass

TIMEOUT_S = 180
_SSL_CTX: ssl.SSLContext | None = None


def _urlopen(req: urllib.request.Request, timeout: float):
    if _SSL_CTX is not None:
        return urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX)
    return urllib.request.urlopen(req, timeout=timeout)


COLUMNS = [
    "테스트일자",
    "ISBN",
    "제목",
    "알라딘카테고리",
    "빠른모드_653",
    "정밀모드_653",
    "빠른모드_소요초",
    "정밀모드_소요초",
    "소요더짧은모드",
    "빠른모드_프롬프트토큰",
    "빠른모드_완성토큰",
    "빠른모드_합계토큰",
    "정밀모드_프롬프트토큰",
    "정밀모드_완성토큰",
    "정밀모드_합계토큰",
    "토큰더적은모드",
    "비고",
]


def _api_base() -> str:
    return os.environ.get("I2M_653_API_BASE", "http://127.0.0.1:8000").rstrip("/")


def _normalize_isbn(raw: str) -> str:
    return re.sub(r"[\s\-]", "", (raw or "").strip())


def _load_isbns(args: argparse.Namespace) -> list[str]:
    isbns: list[str] = []
    if args.isbn_file:
        text = Path(args.isbn_file).read_text(encoding="utf-8-sig")
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # CSV 첫 열·탭·공백 구분
            token = re.split(r"[\t,;]", line)[0].strip()
            n = _normalize_isbn(token)
            if n:
                isbns.append(n)
    for raw in args.isbns:
        n = _normalize_isbn(raw)
        if n:
            isbns.append(n)
    # 순서 유지 중복 제거
    seen: set[str] = set()
    out: list[str] = []
    for x in isbns:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _timed_field653(endpoint: str, isbn: str, mode: str) -> tuple[float, dict]:
    body = json.dumps({"isbn": isbn, "analysis_mode": mode}).encode("utf-8")
    req = urllib.request.Request(endpoint, data=body, headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    try:
        with _urlopen(req, TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        elapsed = time.perf_counter() - t0
        try:
            err_body = e.read().decode("utf-8")[:500]
        except Exception:
            err_body = str(e)
        return elapsed, {"success": False, "error": f"HTTP {e.code}: {err_body}"}
    except Exception as e:
        elapsed = time.perf_counter() - t0
        return elapsed, {"success": False, "error": repr(e)}

    elapsed = time.perf_counter() - t0
    try:
        return elapsed, json.loads(raw)
    except json.JSONDecodeError:
        return elapsed, {"success": False, "error": "invalid JSON"}


def _usage_cells(payload: dict) -> tuple[str, str, str]:
    usage = payload.get("token_usage") or {}
    if not usage:
        return "", "", ""
    return (
        str(usage.get("prompt_tokens") or ""),
        str(usage.get("completion_tokens") or ""),
        str(usage.get("total_tokens") or ""),
    )


def _parse_seconds(cell: str) -> float | None:
    s = (cell or "").strip()
    if not s:
        return None
    m = re.match(r"^(\d+(?:\.\d+)?)", s)
    return float(m.group(1)) if m else None


def _parse_int(cell: str) -> int | None:
    s = (cell or "").strip()
    if not s.isdigit():
        return None
    return int(s)


def _shorter_mode(fast_sec: str, precise_sec: str) -> str:
    f, p = _parse_seconds(fast_sec), _parse_seconds(precise_sec)
    if f is None or p is None:
        return "비교불가"
    if abs(f - p) < 0.001:
        return "동일"
    return "빠른" if f < p else "정밀"


def _fewer_tokens_mode(fast_total: str, precise_total: str) -> str:
    f, p = _parse_int(fast_total), _parse_int(precise_total)
    if f is None or p is None:
        return "비교불가"
    if f == p:
        return "동일"
    return "빠른" if f < p else "정밀"


def _run_benchmark(isbns: list[str], endpoint: str) -> list[dict[str, str]]:
    today = date.today().isoformat()
    rows: list[dict[str, str]] = []
    for isbn in isbns:
        row: dict[str, str] = {c: "" for c in COLUMNS}
        row["테스트일자"] = today
        row["ISBN"] = isbn
        notes: list[str] = []

        fast_payload: dict | None = None
        precise_payload: dict | None = None

        for mode, sec_col, kw_col, p_col, c_col, t_col in (
            ("fast", "빠른모드_소요초", "빠른모드_653", "빠른모드_프롬프트토큰", "빠른모드_완성토큰", "빠른모드_합계토큰"),
            ("precise", "정밀모드_소요초", "정밀모드_653", "정밀모드_프롬프트토큰", "정밀모드_완성토큰", "정밀모드_합계토큰"),
        ):
            elapsed, payload = _timed_field653(endpoint, isbn, mode)
            if mode == "fast":
                fast_payload = payload
            else:
                precise_payload = payload

            if payload.get("success"):
                row[sec_col] = f"{elapsed:.3f}"
                row[kw_col] = payload.get("raw_keyword_line") or payload.get("tag_653") or ""
                pt, ct, tt = _usage_cells(payload)
                row[p_col], row[c_col], row[t_col] = pt, ct, tt
            else:
                err = payload.get("error") or "success=false"
                row[sec_col] = f"{elapsed:.3f} ({err})"
                notes.append(f"{mode}: {err}")
            print(f"  {isbn} {mode}: {row[sec_col]}")

        aladin = (fast_payload or precise_payload or {}).get("aladin") or {}
        if aladin:
            row["제목"] = aladin.get("title") or ""
            row["알라딘카테고리"] = aladin.get("category") or ""

        row["소요더짧은모드"] = _shorter_mode(row["빠른모드_소요초"], row["정밀모드_소요초"])
        row["토큰더적은모드"] = _fewer_tokens_mode(row["빠른모드_합계토큰"], row["정밀모드_합계토큰"])
        row["비고"] = "; ".join(notes)
        rows.append(row)
    return rows


def _write_xlsx(path: Path, rows: list[dict[str, str]]) -> None:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font
    except ImportError as e:
        raise SystemExit(
            "openpyxl이 필요합니다: pip install openpyxl\n"
            "또는 requirements.txt 설치 후 다시 실행하세요."
        ) from e

    wb = Workbook()
    ws = wb.active
    ws.title = "모드비교"
    ws.append(COLUMNS)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for row in rows:
        ws.append([row.get(c, "") for c in COLUMNS])
    for col in ws.columns:
        max_len = 0
        letter = col[0].column_letter
        for cell in col:
            if cell.value is not None:
                max_len = max(max_len, len(str(cell.value)))
        ws.column_dimensions[letter].width = min(max_len + 2, 60)
    wb.save(path)


def main() -> None:
    global _SSL_CTX
    parser = argparse.ArgumentParser(description="빠른/정밀 모드 속도·토큰 비교 → Excel")
    parser.add_argument("isbns", nargs="*", help="ISBN (여러 개 가능)")
    parser.add_argument("--isbn-file", type=Path, help="ISBN 목록 텍스트/CSV (한 줄에 하나)")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="출력 .xlsx 경로 (기본: 프로젝트 루트/653_모드비교_YYYYMMDD.xlsx)",
    )
    parser.add_argument(
        "--api-base",
        default=None,
        help="API 베이스 URL (미지정 시 I2M_653_API_BASE 또는 http://127.0.0.1:8000)",
    )
    parser.add_argument(
        "--insecure-ssl",
        action="store_true",
        help="HTTPS 인증서 검증 생략(로컬 Python SSL 오류 시)",
    )
    args = parser.parse_args()
    if args.insecure_ssl:
        _SSL_CTX = ssl.create_default_context()
        _SSL_CTX.check_hostname = False
        _SSL_CTX.verify_mode = ssl.CERT_NONE
    isbns = _load_isbns(args)
    if not isbns:
        raise SystemExit("ISBN이 없습니다. 인자 또는 --isbn-file 로 지정하세요.")

    api_base = (args.api_base or _api_base()).rstrip("/")
    endpoint = f"{api_base}/api/field653"
    out = args.output
    if out is None:
        out = Path(__file__).resolve().parents[1] / f"653_모드비교_{date.today().strftime('%Y%m%d')}.xlsx"
    out = out.with_suffix(".xlsx")

    print(f"측정 API: {endpoint}")
    print(f"ISBN {len(isbns)}건 → {out}")
    rows = _run_benchmark(isbns, endpoint)
    _write_xlsx(out, rows)
    print(f"저장 완료: {out}")


if __name__ == "__main__":
    main()
