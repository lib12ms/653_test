"""Google Sheets 골든 데이터 저장 서비스."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _get_sheet() -> gspread.Worksheet:
    credentials_json = os.getenv("GOOGLE_SERVICE_ACCOUNT")
    if not credentials_json:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT 환경변수가 설정되지 않았습니다.")
    credentials_dict = json.loads(credentials_json)
    credentials = Credentials.from_service_account_info(credentials_dict, scopes=_SCOPES)
    client = gspread.authorize(credentials)
    sheet_id = os.getenv("GOOGLE_SHEETS_ID")
    if not sheet_id:
        raise ValueError("GOOGLE_SHEETS_ID 환경변수가 설정되지 않았습니다.")
    return client.open_by_key(sheet_id).sheet1


def save_golden_data(data: dict) -> bool:
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
        return True
    except Exception:
        logger.exception("골든 데이터 저장 실패")
        return False
