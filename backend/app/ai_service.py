"""653: 전처리 + OpenAI 의미분석(httpx) + 키워드도출."""
from __future__ import annotations

import logging
import re
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from .config import Settings, get_settings
from .models import AladinMetadata653
from .preprocess import (
    build_forbidden_set,
    clean_author_str,
    norm_text,
    should_keep_keyword,
    validate_keyword,
)

logger = logging.getLogger(__name__)


def _is_openai_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return isinstance(
        exc,
        (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout),
    )


@retry(
    reraise=True,
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=0.7, min=0.7, max=10),
    retry=retry_if_exception(_is_openai_retryable),
)
def _openai_chat_completions(
    api_key: str,
    base_url: str,
    model: str,
    messages: list[dict[str, str]],
    temperature: float = 0.2,
    max_tokens: int = 180,
    timeout: float = 60.0,
) -> str:
    url = f"{base_url.rstrip('/')}/chat/completions"
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        with httpx.Client() as client:
            r = client.post(url, json=payload, timeout=timeout, headers=headers)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as e:
        emsg = str(e).lower()
        if "certificate verify failed" not in emsg and "self-signed" not in emsg:
            raise
        logger.warning("OpenAI SSL 검증 실패로 verify=False 폴백")
        with httpx.Client(verify=False) as client:
            r = client.post(url, json=payload, timeout=timeout, headers=headers)
            r.raise_for_status()
            data = r.json()
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content")
    return (content or "").strip()


def _system_and_user_messages(
    category: str,
    title: str,
    authors: str,
    description: str,
    toc: str,
    max_keywords: int,
) -> tuple[dict[str, str], dict[str, str]]:
    parts = [p.strip() for p in (category or "").split(">") if p.strip()]
    cat_tail = " ".join(parts[-2:]) if len(parts) >= 2 else (parts[-1] if parts else "")

    forbidden = build_forbidden_set(title, authors)
    forbidden_list = ", ".join(sorted(forbidden)) or "(없음)"

    system_msg = {
        "role": "system",
        "content": (
            "당신은 KORMARC 작성 경험이 풍부한 도서관 메타데이터 전문가입니다.\n"
            "주어진 도서 정보를 바탕으로 MARC 653 자유주제어를 생성하세요.\n\n"
            "아래 5단계는 반드시 **내부적으로만** 수행하고, 단계별 사고 과정은 절대 출력하지 마세요.\n\n"
            "[1단계: 정보 분석]\n"
            "- 분류 체인(전체/꼬리), 서명, 저자, 설명, 목차를 종합 분석합니다.\n"
            "- 핵심 주제 및 하위 개념 후보를 내부적으로 충분히 도출합니다.\n"
            "- 정보가 부족하면 분류 체인의 마지막 1~2개 요소를 핵심 기반으로 삼습니다.\n\n"
            "[2단계: 전략적 필터링]\n"
            "- 다음에 해당하는 후보는 즉시 제외합니다.\n"
            "  - 서명(245)·저자(100/700) 유래 단어/표현(부분일치, 활용형 포함)\n"
            "  - 시리즈명, 출판사명, 판/쇄, 연도, 페이지, 가격, 수상, 홍보 문구\n"
            "  - 도서 유통/판매 분류어: 국내도서, 외국도서, 실용서, 단행본, 베스트셀러, 신간, 스테디셀러 등 서지 유통 관련 표현\n"
            "  - 일반적·기능 약한 표현: 연구, 개론, 방법, 사례, 고찰, 문제, 개정판, 서문, 목차, 참고문헌, 저자, 번역, 추천사, 베스트셀러, 안내, 소개, 이론 등\n"
            "- 예외 규칙: 제목/저자 유래어라도 '주제 식별에 필수적인 고유 키워드'라면 허용할 수 있습니다.\n"
            "  - 단, 원형 그대로 쓰지 말고 분야 특화 하위개념으로 반드시 치환합니다.\n"
            "  - 허용 상한은 최대 1~2개입니다.\n"
            "  - 예: '뜨개' → '뜨개기법', '수작업뜨개', '코바늘뜨기'\n"
            "- 형식상 부적절한 후보도 제외합니다.\n"
            "  - 한 글자 단어\n"
            "  - 숫자 중심 토큰\n"
            "  - 특수문자 위주 토큰\n\n"
            "[3단계: 실용 분야 특화 치환]\n"
            "- 추상·평가·메타 표현(예: 사회적의의, 의의, 시사점, 배경, 개관, 개요, 현황, 동향, 의미, 정리, 결론, 서사분석, 비평 등)은 그대로 쓰지 않습니다.\n"
            "- 반드시 실제 내용을 드러내는 구체 하위 개념으로 치환합니다.\n\n"
            "- 기술과학/실용서 분야에서는 인접 장르 확산을 금지합니다.\n"
            "  - 예: 뜨개 도서에서 퀼트/십자수로 확산 금지\n"
            "  - 목차/설명을 근거로 해당 분야 내부의 구체 실천 항목을 추출합니다.\n"
            "  - 예: '$a코바늘뜨기 $a영문도안해석 $a니팅테크닉'\n\n"
            "[4단계: 검색 효용 최적화]\n"
            "- 도서관 이용자 검색 효용이 높은 명사 중심 표현을 선택합니다.\n"
            "- 모든 키워드는 붙여쓰기(공백 없음)로 작성합니다.\n"
            "- 가능하면 2~6글자 복합명사를 우선합니다.\n"
            "- 의미 중복/동의 반복은 1개 대표어로 정리합니다.\n\n"
            "[5단계: 최종 출력]\n"
            f"- 관련성, 구체성, 비중복성, 균형을 기준으로 최대 {max_keywords}개를 확정합니다.\n"
            "- 출력은 반드시 한 줄, 아래 형식만 허용합니다.\n"
            "  `$a키워드1 $a키워드2 $a키워드3 ...`\n"
            "- 쉼표, 번호, 괄호, 줄바꿈, 설명 문장, 접두어(예: '결과:')를 절대 포함하지 마세요.\n\n"
            "추가 규칙:\n"
            "- 내부 사고 과정/근거/단계 설명은 출력 금지\n"
            "- 오직 최종 `$a...` 문자열만 출력\n\n"
            "출력 예시:\n"
            "- 나쁜 출력: '키워드: 감정 조절, 성장'\n"
            "- 좋은 출력: '$a정서조절 $a성장소설'\n\n"
            "만약 유효 키워드가 부족하면:\n"
            "- 분류 꼬리 + 설명/목차에서 가장 구체적인 명사만 보수적으로 선택해 1~3개라도 출력합니다."
        ),
    }
    user_msg = {
        "role": "user",
        "content": (
            f"### 분석 대상 도서\n"
            f"- 분류(전체 체인): \"{category}\"\n"
            f"- 분류(핵심 꼬리): \"{cat_tail}\"\n"
            f"- 제목(245): \"{title}\"\n"
            f"- 저자(100/700): \"{authors}\"\n"
            f"- 설명: \"{description}\"\n"
            f"- 목차: \"{toc}\"\n"
            f"- 제외어 목록: {forbidden_list}\n\n"
            f"### 작업 지시\n"
            f"위 데이터를 바탕으로 내부 5단계 사고를 수행하고, "
            f"653 주제어를 생성하세요.\n"
            f"- 목표 개수: 최소 5개, 최대 {max_keywords}개\n\n"
            f"[유의사항]\n"
            f"- 제목 단어라도 주제 식별에 필수라면 원형 그대로 쓰지 말고 "
            f"구체 하위개념으로 변환해 사용하세요.\n"
            f"  예: '뜨개' → '$a코바늘뜨기', '$a대바늘뜨기'\n"
            f"- 카테고리 인접 분야(예: 뜨개 도서에서 퀼트·십자수)로 확산하지 마세요.\n"
            f"- 출력은 최종 결과 한 줄만 허용합니다.\n"
            f"- 설명문, 번호, 괄호, 줄바꿈, '결과:' 같은 접두어를 넣지 마세요.\n\n"
            f"결과 형식: `$a키워드1 $a키워드2 ...`"
        ),
    }
    return system_msg, user_msg


def parse_keyword_line(raw: str) -> list[str]:
    """GPT 응답에서 $a… 패턴(및 백업 파싱)으로 키워드 나열."""
    pattern = re.compile(r"\$a(.*?)(?=(?:\$a|$))", re.DOTALL)
    kws = [m.group(1).strip() for m in pattern.finditer(raw)]
    if not kws:
        tmp = re.split(r"[,\n;|/·]", raw)
        kws = [t.strip().lstrip("$a") for t in tmp if t.strip()]
    kws = [kw.replace(" ", "") for kw in kws if kw]
    return kws


def _extract_backup_candidates(category: str, toc: str, description: str) -> list[str]:
    """GPT 결과가 부족할 때 보강 후보를 추출한다."""
    text = " ".join([category or "", toc or "", description or ""])
    tokens = re.findall(r"[가-힣A-Za-z]{2,12}", text)
    deny = {
        "목차", "차례", "서론", "결론", "저자", "작가", "소개", "연구", "방법", "이론", "문학",
        "한국", "세계", "도서", "작품", "출판", "분석", "개요", "현황", "의의", "시사점",
    }
    out: list[str] = []
    for t in tokens:
        w = t.replace(" ", "")
        if len(w) < 2 or len(w) > 10:
            continue
        if w in deny:
            continue
        out.append(w)
    return out


def finalize_653(
    ai_output: str,
    forbidden_set: set[str],
    max_keywords: int = 7,
    min_keywords: int = 5,
    category: str = "",
    toc: str = "",
    description: str = "",
) -> str:
    """
    AI가 뱉은 결과물에서 금지어/한 글자 단어를 최종 제거하고 $a 형식으로 반환.
    """
    keywords = [k.strip() for k in ai_output.split("$a") if k.strip()]

    # 기본적으로 작가소개성 키워드는 제외(단, 전기/평전/작가론 성격은 예외)
    author_bio_like = {
        "작가", "저자", "등단", "수상", "작품세계", "문단", "생애", "인터뷰", "연보", "약력",
    }
    cat_norm = norm_text(category)
    allow_bio = any(t in cat_norm for t in ("전기", "평전", "작가론", "인물", "회고록"))

    valid_keywords: list[str] = []
    seen: set[str] = set()
    for kw in keywords:
        if validate_keyword(kw, forbidden_set):
            n = norm_text(kw)
            if not allow_bio and any(b in n for b in author_bio_like):
                continue
            if n in seen:
                continue
            seen.add(n)
            valid_keywords.append(kw.replace(" ", ""))

    if len(valid_keywords) < min_keywords:
        backup = _extract_backup_candidates(category, toc, description)
        for kw in backup:
            n = norm_text(kw)
            if n in seen:
                continue
            if not validate_keyword(kw, forbidden_set):
                continue
            if not allow_bio and any(b in n for b in author_bio_like):
                continue
            seen.add(n)
            valid_keywords.append(kw)
            if len(valid_keywords) >= min_keywords:
                break

    return "".join([f"$a{k}" for k in valid_keywords[:max_keywords]])


def generate_653_subfield_line(
    meta: AladinMetadata653,
    max_keywords: int = 7,
    min_keywords: int = 5,
    settings: Settings | None = None,
) -> tuple[str | None, str | None]:
    """
    Returns (raw $a...$a... line without '=653' prefix, None on failure)
    and error message.
    """
    s = get_settings() if settings is None else settings
    if not s.openai_api_key:
        return None, "OPENAI_API_KEY가 설정되지 않았습니다."

    category = meta.category
    title = meta.title
    authors = clean_author_str(meta.authors)
    description = meta.description
    toc = meta.toc

    sys_m, user_m = _system_and_user_messages(
        category, title, authors, description, toc, max_keywords
    )
    try:
        raw = _openai_chat_completions(
            s.openai_api_key,
            s.openai_base_url,
            s.openai_model,
            [sys_m, user_m],
            temperature=0.2,
            max_tokens=180,
            timeout=60.0,
        )
    except Exception as e:
        logger.exception("OpenAI 653 호출 실패")
        return None, str(e)

    forbidden = build_forbidden_set(title, authors)
    kws = parse_keyword_line(raw)
    ai_output = "".join(f"$a{kw}" for kw in kws if should_keep_keyword(kw, forbidden))
    subfield_line = finalize_653(
        ai_output,
        forbidden,
        max_keywords=max_keywords,
        min_keywords=min_keywords,
        category=category,
        toc=toc,
        description=description,
    )
    if not subfield_line:
        return None, "유효한 키워드를 추출하지 못했습니다."

    return subfield_line, None


def build_marc_653_line(subfield_line: str) -> str:
    """'$a..$a..' → MRK 한 줄(1215_main `=653  \\\\` + 서브필드 꼴)."""
    compact = subfield_line.replace(" ", "")
    return f"=653  \\\\{compact}"
