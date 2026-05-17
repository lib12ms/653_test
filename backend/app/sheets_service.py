"""Google Sheets 골든 데이터 저장 서비스."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime

import gspread

logger = logging.getLogger(__name__)

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


def _get_sheet() -> gspread.Worksheet:
    credentials_json = os.getenv("GOOGLE_SERVICE_ACCOUNT")
    if not credentials_json:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT 환경변수가 설정되지 않았습니다.")
    credentials_dict = json.loads(credentials_json)
    client = gspread.service_account_from_dict(credentials_dict, scopes=_SCOPES)
    sheet_id = os.getenv("GOOGLE_SHEETS_ID")
    if not sheet_id:
        raise ValueError("GOOGLE_SHEETS_ID 환경변수가 설정되지 않았습니다.")
    return client.open_by_key(sheet_id).sheet1


def diagnose_sheets() -> dict:
    """연결 상태 진단 — /api/sheets-check 에서 호출."""
    import traceback
    result: dict = {
        "env_account": bool(os.getenv("GOOGLE_SERVICE_ACCOUNT")),
        "env_sheet_id": os.getenv("GOOGLE_SHEETS_ID", ""),
        "json_parse": False,
        "client_ok": False,
        "open_ok": False,
        "error": "",
    }
    try:
        credentials_json = os.getenv("GOOGLE_SERVICE_ACCOUNT", "")
        credentials_dict = json.loads(credentials_json)
        result["json_parse"] = True
        result["sa_email"] = credentials_dict.get("client_email", "")

        client = gspread.service_account_from_dict(credentials_dict, scopes=_SCOPES)
        result["client_ok"] = True

        sheet_id = os.getenv("GOOGLE_SHEETS_ID", "")
        sh = client.open_by_key(sheet_id)
        result["open_ok"] = True
        result["sheet_title"] = sh.title
    except Exception as e:
        result["error"] = traceback.format_exc()
    return result


def save_golden_data(data: dict) -> tuple[bool, str]:
    try:
        sheet = _get_sheet()
        row = [
            data.get("isbn", ""),
            data.get("title", ""),
            data.get("authors", ""),
            data.get("category", ""),
            data.get("category_group", ""),
            data.get("gpt_result", ""),
            data.get("golden_result", ""),
            "Y" if data.get("is_modified") else "N",
            datetime.now().strftime("%Y-%m-%d"),
            data.get("mode", ""),
        ]
        sheet.append_row(row)
        return True, ""
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        logger.exception("골든 데이터 저장 실패")
        return False, msg
