"""현재 프롬프트의 실제 토큰 수를 OpenAI Responses API로 측정."""
import asyncio
import os
import sys

sys.path.insert(0, ".")
os.environ["ALLOW_INSECURE_SSL_FALLBACK"] = "true"

from app.ai_service import _STATIC_INSTRUCTIONS, _build_input, _get_openai_client
from app.config import get_settings

cases = [
    ("소설/시/희곡>한국소설>2000년대이후한국소설", "문학"),
    ("자기계발>성공/처세", "자기계발"),
    ("컴퓨터/모바일>프로그래밍언어>파이썬", "기술과학"),
    ("인문학>철학>서양철학", "인문학"),
    ("사회과학>사회학>사회문제", "사회과학"),
]


async def main() -> None:
    s = get_settings()
    client = _get_openai_client(s)

    print(f"{'분야':<10}  입력토큰  출력토큰  합계")
    print("-" * 45)
    total = 0
    for category, label in cases:
        input_text = _build_input(
            category=category,
            title="테스트제목",
            authors="저자명",
            description="책 설명입니다.",
            toc="1장 목차",
            max_keywords=7,
        )
        resp = await client.responses.create(
            model=s.openai_model,
            instructions=_STATIC_INSTRUCTIONS,
            input=input_text,
            max_output_tokens=1,
        )
        u = resp.usage
        total += u.input_tokens
        print(f"{label:<10}  {u.input_tokens:>8,}  {u.output_tokens:>8}  {u.input_tokens + u.output_tokens:>6,}")

    avg = total // len(cases)
    print("-" * 45)
    print(f"{'평균':<10}  {avg:>8,}")


if __name__ == "__main__":
    asyncio.run(main())
