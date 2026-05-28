"""
KORMARC 653 Responses API 스모크 테스트.

API 키와 _STATIC_INSTRUCTIONS가 올바르게 작동하는지 확인합니다.

사용:
  cd backend
  python scripts/initialize_agent.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.ai_service import _STATIC_INSTRUCTIONS, _get_openai_client
from app.config import get_settings

_SMOKE_INPUT = (
    "[카테고리 그룹: 인문학]\n"
    "[카테고리별 지침]\n(테스트)\n"
    "### 분석 대상 도서\n"
    '- 분류(전체 체인): "국내도서>인문학>철학>서양철학"\n'
    '- 제목(245): "피로사회"\n'
    '- 저자(100/700): "한병철"\n'
    '- 설명: "현대 사회의 피로와 번아웃"\n'
    '- 목차: ""\n'
    "- 제외어 목록: (없음)\n\n"
    "출력: 최소 5개, 최대 7개"
)


async def smoke_test() -> None:
    settings = get_settings()
    if not settings.openai_api_key:
        raise SystemExit("OPENAI_API_KEY가 .env에 설정되지 않았습니다.")

    client = _get_openai_client(settings)
    model = settings.openai_model

    print(f"모델: {model}")
    print(f"지침 길이: {len(_STATIC_INSTRUCTIONS):,}자")
    print("Responses API 스모크 호출 중…")

    resp = await client.responses.create(
        model=model,
        instructions=_STATIC_INSTRUCTIONS,
        input=_SMOKE_INPUT,
        max_output_tokens=120,
    )

    print(f"출력: {(resp.output_text or '').strip()}")
    if resp.usage:
        print(
            f"토큰: in={resp.usage.input_tokens} "
            f"out={resp.usage.output_tokens} "
            f"total={resp.usage.input_tokens + resp.usage.output_tokens}"
        )


def main() -> None:
    asyncio.run(smoke_test())


if __name__ == "__main__":
    main()
