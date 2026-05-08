"""653: 전처리 + OpenAI 의미분석(httpx) + 키워드도출."""
from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from .config import Settings, get_settings
from .models import AladinMetadata653, AnalysisMode
from .preprocess import (
    build_forbidden_set,
    clean_author_str,
    norm_text,
    should_keep_keyword,
    validate_keyword,
)

logger = logging.getLogger(__name__)


CATEGORY_PROMPTS = {
    "문학": (
        "이 책은 문학 작품입니다.\n"
        "- 작품의 배경(시대적/지역적), 문학적 기법, 주제의식 위주로 추출하세요.\n"
        "- 예: '식민지문학', '여성서사', '성장소설', '실존주의', '사랑서사', '언어의식'\n"
        "- 단순 장르명(소설, 시, 에세이)이나 출간 시기 라벨은 제외하세요.\n"
        "- '사랑의형상', '감정조각', '문학적탐구' 같은 평론 문구는 명사형 주제어로 치환하세요.\n"
    ),
    "인문학": (
        "이 책은 인문학 도서입니다.\n"
        "- 사상적 개념, 역사적 사건/시대, 철학적 주제어 위주로 추출하세요.\n"
        "- 예: '근현대사', '실존주의', '동양철학', '문명비판'\n"
        "- 글쓰기/창작/출판 관련 도서라면 매체 환경, 창작 윤리, 저작권, 콘텐츠 생산 방식의 구체어를 선택하세요.\n"
        "- 예: '생성형AI', 'AI글쓰기', '저작권', '창작윤리', '콘텐츠창작'\n"
        "- 너무 포괄적인 표현(역사, 철학)보다 구체적 하위개념을 선택하세요.\n"
    ),
    "종교/역학": (
        "이 책은 종교 또는 역학 도서입니다.\n"
        "- 종파명, 교리 개념, 수행 방법, 역학 이론 위주로 추출하세요.\n"
        "- 예: '불교명상', '기독교윤리', '사주명리', '풍수지리'\n"
        "- 특정 종교/역학 체계를 드러내는 구체적 용어를 선택하세요.\n"
    ),
    "사회과학": (
        "이 책은 사회과학 도서입니다.\n"
        "- 사회현상, 제도, 이론적 개념, 연구대상 위주로 추출하세요.\n"
        "- 예: '노동시장', '젠더정치', '복지국가', '조직행동론', '가족주의', '가족정책'\n"
        "- 제목의 핵심어가 사회과학 연구대상이라면 원형 반복 대신 복합 주제어로 치환하세요.\n"
        "- 예: '가족' → '가족주의', '가족유형', '가족사회학', '가족정책'\n"
        "- '사회과학', '사회문제', '사회문제일반' 같은 상위 분류명은 제외하세요.\n"
        "- 경제경영 도서라면 산업/시장/전략 관련 구체적 용어를 선택하세요.\n"
        "- 예: '스타트업전략', '마케팅심리', '재무관리'\n"
    ),
    "자기계발": (
        "이 책은 자기계발 도서입니다.\n"
        "- 실천 가능한 구체적 행동 개념, 심리 기제 위주로 추출하세요.\n"
        "- 예: '시간관리', '습관형성', '감정조절', '목표설정'\n"
        "- '성공', '행복' 같은 추상적 표현은 반드시 하위개념으로 치환하세요.\n"
    ),
    "자연과학": (
        "이 책은 자연과학 도서입니다.\n"
        "- 과학적 이론, 연구 분야, 핵심 개념어 위주로 추출하세요.\n"
        "- 예: '양자역학', '진화생물학', '뇌과학', '기후변화', '우주과학'\n"
        "- 분류 꼬리의 연구 분야(예: 천문학, 우주과학)를 우선 반영하세요.\n"
        "- 일반 독자 대상이라면 대중과학 관점의 핵심어를 선택하세요.\n"
        "- '세계관', '과학탐구', '과학적논리'처럼 너무 포괄적이거나 메타적인 표현은 제외하세요.\n"
    ),
    "기술과학": (
        "이 책은 기술과학 또는 실용 도서입니다.\n"
        "- 구체적 기법, 도구명, 실천 항목 위주로 추출하세요.\n"
        "- 예: '머신러닝', '코바늘뜨기', '비건요리', '근력운동', '제미나이', '노트북LM'\n"
        "- AI/컴퓨터 실습서는 제목·설명에 명시된 서비스명, 도구명, 작업명을 우선 선택하세요.\n"
        "- 예: '제미나이', '노트북LM', '딥리서치', 'AI코딩', '구글워크스페이스'\n"
        "- 기술서의 도구명은 제목 유래어라도 핵심 검색어이므로 반드시 허용하세요.\n"
        "- 제목/설명에 여러 도구명이 있으면 실제 도구명 2~4개를 우선 포함하세요.\n"
        "- 설명에 없는 인접 기술어(예: 딥러닝)는 추정해서 넣지 마세요.\n"
        "- 인접 분야(예: 뜨개 도서에서 퀼트/십자수)로 확산하지 마세요.\n"
        "- 목차의 구체적 내용을 최대한 반영하세요.\n"
    ),
    "예술": (
        "이 책은 예술 도서입니다.\n"
        "- 예술 장르, 기법, 사조, 작가/작품 관련 개념어 위주로 추출하세요.\n"
        "- 예: '인상주의', '현대무용', '대중음악사', '영화미학'\n"
        "- 특정 예술 분야를 드러내는 구체적 용어를 선택하세요.\n"
    ),
    "교육": (
        "이 책은 교육 또는 외국어 도서입니다.\n"
        "- 학습 대상 언어/과목, 교육 방법론, 학습 단계 위주로 추출하세요.\n"
        "- 예: '영어회화', '수능국어', '학습심리', 'TOPIK'\n"
        "- 대학교재라면 해당 학문 분야의 핵심 개념어를 선택하세요.\n"
    ),
    "기타": (
        "이 책은 특정 분야에 한정되지 않는 도서입니다.\n"
        "- 분류·설명·목차에서 핵심 주제를 균형있게 추출하세요.\n"
        "- 예: '세계여행', '육아심리', '동양고전'\n"
        "- 너무 포괄적인 표현보다 책의 실제 내용을 드러내는 구체어를 선택하세요.\n"
    ),
}

CATEGORY_MAP = {
    "소설": "문학",
    "시": "문학",
    "희곡": "문학",
    "에세이": "문학",
    "장르소설": "문학",
    "인문학": "인문학",
    "역사": "인문학",
    "종교": "종교/역학",
    "역학": "종교/역학",
    "사회과학": "사회과학",
    "경제경영": "사회과학",
    "자기계발": "자기계발",
    "과학": "자연과학",
    "컴퓨터": "기술과학",
    "모바일": "기술과학",
    "건강": "기술과학",
    "취미": "기술과학",
    "요리": "기술과학",
    "살림": "기술과학",
    "예술": "예술",
    "대중문화": "예술",
    "대학교재": "교육",
    "외국어": "교육",
    "여행": "기타",
    "전집": "기타",
    "좋은부모": "기타",
}

LOW_VALUE_KEYWORDS = {
    "취미",
    "건강정보",
    "사회과학",
    "사회문제",
    "사회문제일반",
    "2000년대이후",
    "문학적탐구",
    "사랑의형상",
    "사랑형상",
    "감정조각",
    "문학적형상",
    "소설/시/희곡",
    "활기찬노년",
    "품격노년",
    "품격있는노년",
    "치과의사팁",
    "오래",
    "사는",
    "아프지",
    "보내는",
    "주제어힌트",
    "nlk키워드",
    "nlk목차url",
    "nlk소개url",
    "국립중앙도서관kdc",
    "핵심도구",
    "딥러닝",
    "구글계정생성",
}

CONTENT_FORMAT_TOKENS = ("팁", "비결", "상식", "추천", "모음")
NATURAL_PHRASE_PREFIXES = ("활기찬", "품격있는", "특별한")
LOW_VALUE_SUFFIXES = ("적조명", "특별함")
NATURAL_SCIENCE_LOW_VALUE_KEYWORDS = {
    "세계관",
    "세계구성",
    "과학탐구",
    "과학적논리",
    "과학논리",
}

CATEGORY_CANDIDATE_DENY = {
    "과학",
    "문학",
    "역사",
    "철학",
    "건강",
    "취미",
    "다이어트",
    "건강정보",
}


def get_category_group(category: str) -> str:
    """알라딘 카테고리 문자열에서 대분류를 찾아 반환합니다."""
    parts = [part.strip() for part in (category or "").split(">") if part.strip()]
    for part in parts:
        for key, group in CATEGORY_MAP.items():
            if key in part:
                return group
    for key, group in CATEGORY_MAP.items():
        if key in category:
            return group
    return "기타"


def get_category_prompt(category: str) -> str:
    """대분류에 맞는 프롬프트를 반환합니다."""
    group = get_category_group(category)
    return CATEGORY_PROMPTS.get(group, CATEGORY_PROMPTS["기타"])


def _is_low_value_keyword(normalized_keyword: str, category_group: str = "") -> bool:
    compact = normalized_keyword.replace(" ", "")
    if compact in LOW_VALUE_KEYWORDS:
        return True
    if re.fullmatch(r"\d{4}년대이후.*", compact):
        return True
    if category_group == "자연과학" and compact in NATURAL_SCIENCE_LOW_VALUE_KEYWORDS:
        return True
    if "url" in compact or "kdc" in compact:
        return True
    if any(token in compact for token in CONTENT_FORMAT_TOKENS):
        return True
    if any(compact.startswith(prefix) for prefix in NATURAL_PHRASE_PREFIXES):
        return True
    if any(compact.endswith(suffix) for suffix in LOW_VALUE_SUFFIXES):
        return True
    return False


def _can_use_insecure_fallback(base_url: str, settings: Settings) -> bool:
    if not settings.allow_insecure_ssl_fallback:
        return False
    allow_hosts = settings.insecure_ssl_fallback_hosts
    if not allow_hosts:
        return False
    host = (urlparse(base_url).hostname or "").lower()
    return host in allow_hosts


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
async def _openai_chat_completions(
    api_key: str,
    base_url: str,
    model: str,
    messages: list[dict[str, str]],
    settings: Settings,
    client: httpx.AsyncClient | None = None,
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
    req_client = client or httpx.AsyncClient()
    owns_client = client is None
    try:
        r = await req_client.post(url, json=payload, timeout=timeout, headers=headers)
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPError as e:
        emsg = str(e).lower()
        if "certificate verify failed" not in emsg and "self-signed" not in emsg:
            raise
        if not _can_use_insecure_fallback(base_url, settings):
            raise
        logger.warning("OpenAI SSL 검증 실패로 제한적 verify=False 폴백")
        async with httpx.AsyncClient(verify=False) as insecure_client:
            r = await insecure_client.post(url, json=payload, timeout=timeout, headers=headers)
            r.raise_for_status()
            data = r.json()
    finally:
        if owns_client:
            await req_client.aclose()
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content")
    return (content or "").strip()


def _system_and_user_messages(
    category: str,
    title: str,
    authors: str,
    description: str,
    toc: str,
    max_keywords: int,
    analysis_mode: AnalysisMode,
) -> tuple[dict[str, str], dict[str, str]]:
    parts = [p.strip() for p in (category or "").split(">") if p.strip()]
    cat_tail = " ".join(parts[-2:]) if len(parts) >= 2 else (parts[-1] if parts else "")

    forbidden = build_forbidden_set(title, authors)
    forbidden_list = ", ".join(sorted(forbidden)) or "(없음)"
    category_group = get_category_group(category)
    category_prompt = get_category_prompt(category)

    if analysis_mode == "precise":
        mode_prompt = (
            "정밀 모드로 수행합니다.\n"
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
            "[3단계: 분야 특화 치환]\n"
            "- 추상·평가·메타 표현(예: 사회적의의, 의의, 시사점, 배경, 개관, 개요, 현황, 동향, 의미, 정리, 결론, 서사분석, 비평 등)은 그대로 쓰지 않습니다.\n"
            "- 반드시 실제 내용을 드러내는 구체 하위 개념으로 치환합니다.\n"
            "- 카테고리별 지침을 우선 적용하고, 인접 분야로 근거 없이 확산하지 않습니다.\n\n"
            "[4단계: 검색 효용 최적화]\n"
            "- 도서관 이용자 검색 효용이 높은 명사 중심 표현을 선택합니다.\n"
            "- 모든 키워드는 붙여쓰기(공백 없음)로 작성합니다.\n"
            "- 각 키워드는 하나의 독립된 명사형 주제어 또는 굳어진 복합명사여야 합니다.\n"
            "- 형용사/관형사+명사 형태의 자연어 문구는 주제어로 쓰지 않습니다.\n"
            "- 가능하면 2~6글자 복합명사를 우선합니다.\n"
            "- 의미 중복/동의 반복은 1개 대표어로 정리합니다.\n\n"
            "[5단계: 최종 출력]\n"
            f"- 관련성, 구체성, 비중복성, 균형을 기준으로 최대 {max_keywords}개를 확정합니다.\n"
        )
    else:
        mode_prompt = (
            "빠른 모드로 수행합니다.\n"
            "- 분류 꼬리, 설명, 목차에서 가장 직접적인 핵심 주제를 빠르게 선별합니다.\n"
            "- 제목/저자 유래어, 홍보 문구, 지나치게 일반적인 표현은 제외합니다.\n"
            "- 추상어는 실제 내용을 드러내는 구체 하위개념으로 치환합니다.\n"
            f"- 중복 없이 최대 {max_keywords}개를 확정합니다.\n"
        )

    system_msg = {
        "role": "system",
        "content": (
            "당신은 KORMARC 작성 경험이 풍부한 도서관 메타데이터 전문가입니다.\n"
            "주어진 도서 정보를 바탕으로 MARC 653 자유주제어를 생성하세요.\n\n"
            f"{mode_prompt}\n"
            f"카테고리 그룹: {category_group}\n"
            f"[카테고리별 지침]\n{category_prompt}\n"
            "- 출력은 반드시 한 줄, 아래 형식만 허용합니다.\n"
            "  `$a키워드1 $a키워드2 $a키워드3 ...`\n"
            "- 쉼표, 번호, 괄호, 줄바꿈, 설명 문장, 접두어(예: '결과:')를 절대 포함하지 마세요.\n\n"
            "추가 규칙:\n"
            "- 내부 사고 과정/근거/단계 설명은 출력 금지\n"
            "- 오직 최종 `$a...` 문자열만 출력\n\n"
            "- 'NLK키워드', '주제어힌트', 'URL', 'KDC' 같은 메타 라벨은 키워드로 쓰지 마세요.\n"
            "- '취미', '건강정보' 같은 상위 분류명은 더 구체적인 하위개념으로 치환하세요.\n\n"
            "- '활기찬노년', '품격있는노년', '품격노년' 같은 수식어+명사 표현은 쓰지 마세요.\n"
            "- '팁', '비결', '상식', '추천' 같은 콘텐츠 형식어는 키워드에 포함하지 마세요.\n"
            "- '오래', '사는' 같은 동사/부사성 단어 조각은 키워드로 쓰지 마세요.\n"
            "- '철학적조명', '법적조명', '실용적조명'처럼 '~적조명' 형태의 서술 표현은 쓰지 마세요.\n"
            "- 예: '활기찬노년' → '노년건강', '시니어건강', '건강노화'\n"
            "- 예: '치과의사팁' → '구강건강', '잇몸건강'\n\n"
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
            f"위 데이터를 바탕으로 {analysis_mode} 모드와 카테고리별 지침을 적용해 "
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
        "단순히", "구체적", "올바른", "작은", "변화", "모음", "상식", "추천",
        "활기찬", "품격있는", "품격노년", "치과의사팁", "비결", "팁",
        "오래", "사는", "아프지", "보내는",
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


def _extract_category_candidates(category: str) -> list[str]:
    """분류 체인의 구체 하위 분야명을 보강 후보로 사용한다."""
    candidates: list[str] = []
    for part in reversed([p.strip() for p in (category or "").split(">") if p.strip()]):
        if "국립중앙도서관" in part or "kdc" in norm_text(part):
            continue
        if "/" in part:
            continue
        token = part.replace(" ", "")
        n = norm_text(token).replace(" ", "")
        if len(token) < 2 or len(token) > 10:
            continue
        if n in CATEGORY_CANDIDATE_DENY:
            continue
        candidates.append(token)
    return candidates


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
    category_group = get_category_group(category)

    valid_keywords: list[str] = []
    seen: set[str] = set()
    for kw in keywords:
        if validate_keyword(kw, forbidden_set):
            n = norm_text(kw)
            if _is_low_value_keyword(n, category_group):
                continue
            if not allow_bio and any(b in n for b in author_bio_like):
                continue
            if n in seen:
                continue
            seen.add(n)
            valid_keywords.append(kw.replace(" ", ""))

    if not valid_keywords:
        backup = _extract_backup_candidates(category, toc, description)
        for kw in backup:
            n = norm_text(kw)
            if n in seen:
                continue
            if _is_low_value_keyword(n, category_group):
                continue
            if not validate_keyword(kw, forbidden_set):
                continue
            if not allow_bio and any(b in n for b in author_bio_like):
                continue
            seen.add(n)
            valid_keywords.append(kw)
            if len(valid_keywords) >= min_keywords:
                break

    if len(valid_keywords) < min_keywords:
        for kw in _extract_category_candidates(category):
            n = norm_text(kw)
            if n in seen:
                continue
            if _is_low_value_keyword(n, category_group):
                continue
            seen.add(n)
            valid_keywords.append(kw)
            if len(valid_keywords) >= min_keywords:
                break

    return "".join([f"$a{k}" for k in valid_keywords[:max_keywords]])


async def generate_653_subfield_line(
    meta: AladinMetadata653,
    max_keywords: int = 7,
    min_keywords: int = 5,
    analysis_mode: AnalysisMode = "fast",
    settings: Settings | None = None,
    client: httpx.AsyncClient | None = None,
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
        category, title, authors, description, toc, max_keywords, analysis_mode
    )
    try:
        max_tokens = 220 if analysis_mode == "precise" else 180
        raw = await _openai_chat_completions(
            s.openai_api_key,
            s.openai_base_url,
            s.openai_model,
            [sys_m, user_m],
            settings=s,
            client=client,
            temperature=0.2,
            max_tokens=max_tokens,
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
