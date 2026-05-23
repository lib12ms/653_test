"""
KORMARC 653 agent Conversation 초기화 (최초 1회 또는 지침 변경 시 주 1회).

developer 역할로 _STATIC_INSTRUCTIONS를 Conversation에 주입한 뒤 conv ID를 출력합니다.
프로젝트 루트 .env 에 KORMARC_AGENT_CONV_ID=conv_… 를 추가하세요.

사용:
  cd backend
  python scripts/initialize_agent.py
  python scripts/initialize_agent.py --verify   # 생성 후 1회 스모크 호출
"""
from __future__ import annotations

import argparse
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
    "출력: `$a키워드1 $a키워드2 ...` 한 줄 (최소 5개, 최대 7개, 사고 과정 없이 결과만)"
)


def _build_client(settings):
    return _get_openai_client(settings)


async def initialize(*, verify: bool) -> str:
    settings = get_settings()
    if not settings.openai_api_key:
        raise SystemExit("OPENAI_API_KEY가 .env에 설정되지 않았습니다.")

    client = _build_client(settings)
    model = settings.openai_model

    print("1/3 Conversation 생성 중…")
    conv = await client.conversations.create(
        metadata={
            "app": "i2m-653",
            "purpose": "kormarc-agent",
            "model": model,
        },
    )
    conv_id = conv.id
    print(f"   → conversation_id: {conv_id}")

    print("2/3 developer 지침 주입 중…")
    await client.conversations.items.create(
        conversation_id=conv_id,
        items=[
            {
                "type": "message",
                "role": "developer",
                "content": _STATIC_INSTRUCTIONS,
            },
        ],
    )
    print(f"   → 지침 길이: {len(_STATIC_INSTRUCTIONS):,}자")

    if verify:
        print("3/3 스모크 호출 (Responses API)…")
        resp = await client.responses.create(
            model=model,
            conversation=conv_id,
            input=_SMOKE_INPUT,
            max_output_tokens=120,
            temperature=0.2,
        )
        print(f"   → 출력 미리보기: {(resp.output_text or '')[:200]}")
        if resp.usage:
            print(
                f"   → 토큰: in={resp.usage.input_tokens} "
                f"out={resp.usage.output_tokens}"
            )
    else:
        print("3/3 스모크 호출 생략 (--verify 로 실행 가능)")

    return conv_id


def main() -> None:
    parser = argparse.ArgumentParser(description="KORMARC 653 agent Conversation 초기화")
    parser.add_argument(
        "--verify",
        action="store_true",
        help="생성 후 Responses API 스모크 호출 1회",
    )
    args = parser.parse_args()

    conv_id = asyncio.run(initialize(verify=args.verify))

    print()
    print("=" * 60)
    print("초기화 완료")
    print("=" * 60)
    print(f"KORMARC_AGENT_CONV_ID={conv_id}")
    print()
    print("다음 단계:")
    print("  1) 프로젝트 루트 .env 에 위 변수를 추가하거나 갱신")
    print("  2) FIELD653_CACHE_BUNDLE_VERSION 을 1 증가 (캐시 무효화)")
    print("  3) 백엔드(uvicorn) 재시작")
    print()
    print("운영 참고:")
    print("  - 지침(_STATIC_INSTRUCTIONS) 변경 시 이 스크립트를 다시 실행하고 conv ID 교체")
    print("  - 로컬 PC와 Render 등 배포 환경은 각각 .env 가 다르면")
    print("    동일 conv 를 쓰거나, 환경마다 1회씩 실행해 별도 conv 를 둘 수 있음")
    print("  - 같은 OpenAI 조직·키를 쓰면 conv ID 하나를 여러 환경에서 공유 가능")


if __name__ == "__main__":
    main()
