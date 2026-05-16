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
from .models import AladinMetadata653, Field653Quality, TokenUsage
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
        "이 책은 문학(소설, 시, 에세이) 작품입니다.\n"
        "- **[핵심 원칙]** '따뜻한', '여운', '감동', '일상' 같은 추상적이거나 형용사적인 감상어는 배제하십시오.\n"
        "- 대신, 감정을 유발하는 **'구체적 소재'**나 **'사회적 상황/페르소나'**를 추출하십시오.\n"
        "- 예: '어린시절' → '90년대', '시골생활' / '가족사랑' → '부모님간병', '조부모' / '추억' → '첫사랑', '고향'\n"
        "- **[배경 키워드 필수 발굴]** 작품의 시대적·지역적 배경은 독자의 핵심 검색 경로입니다. "
        "설명·목차에서 반드시 발굴하여 포함하십시오.\n"
        "- 예(시대): '6·25전쟁', '일제강점기', '1980년대광주', '조선시대', '경성', '1970년대농촌'\n"
        "- 예(인물군·사회계층): '위안부', '노동자계급', '모던걸', '지식인', '이민자'\n"
        "- 문학적 기법·주제의식도 명사형으로 추출하십시오.\n"
        "- 예: '여성서사', '성장소설', '실존주의', '식민지문학'\n"
        "- 단순 장르명(소설, 시)이나 출간 시기 라벨은 제외하십시오.\n"
        "- '사랑의형상', '감정조각', '문학적탐구' 같은 평론 문구는 명사형 주제어로 치환하십시오.\n"
    ),
    "에세이": (
        "이 책은 에세이입니다.\n"
        "- '따뜻한', '여운', '감성', '힐링', '위로', '공감', '소소한', '잔잔한' 같은\n"
        "  형용사적·감상적 표현은 절대 키워드로 쓰지 마세요.\n"
        "- 그 감정을 일으키는 구체적 소재(예: 반려견, 이별, 여행지, 골목, 계절)나\n"
        "  사회적 신분·상황(예: 워킹맘, 투병기, 육아일상, 이민생활, 간호사일상)을 우선 추출하세요.\n"
        "- 저자의 직업·삶의 조건이 뚜렷하다면 그 키워드를 포함하세요(예: 제주살이, 싱글라이프, 노년일상).\n"
        "- 시대적·지역적 배경이 있다면 포함하세요(예: 1970년대, 농촌, 경성, 이민사회).\n"
        "- 단순 장르명(에세이, 수필)이나 평론·홍보 문구는 제외하세요.\n"
    ),
    "인문학": (
        "이 책은 인문학 도서입니다.\n"
        "- 사상적 개념, 역사적 사건/시대, 철학적 주제어 위주로 추출하세요.\n"
        "- 예: '근현대사', '실존주의', '동양철학', '문명비판'\n"
        "- **[철학자·사상가 처리 원칙]** 철학자·사상가명이 제목이나 저자에 이미 있다면 "
        "단독 인명 대신 반드시 '사상가명+분야' 결합어로 치환하세요. "
        "(예: 칸트 → '칸트윤리학' / 헤겔 → '헤겔변증법' / 공자 → '공자인의' / 노자 → '노자무위')\n"
        "- 제목과 저자 모두에 없는 사상가명은 단독으로도 키워드로 허용합니다.\n"
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
        "- **[핵심 원칙]** '성공', '행복', '긍정적인', '용기있는', '특별한', '열정적' 같은\n"
        "  추상적·형용사적 동기부여 어구는 반드시 구체 하위개념으로 치환하세요.\n"
        "- 예: '긍정적사고' → '인지재구성', '자기효능감' / '용기있는도전' → '도전심리', '실패극복'\n"
        "  '행복한삶' → '웰빙', '삶의만족도'\n"
    ),
    "자연과학": (
        "이 책은 자연과학 도서입니다.\n"
        "- 분류 꼬리의 연구 분야(예: 천문학, 우주과학)를 우선 반영하세요.\n"
        "- **[독자 수준 분기]** 제목·분류에 '입문', '쉽게', '교양', '이야기로' 같은 단서가 있으면 "
        "일반 독자 대상 대중어를 우선 선택하세요. (예: '뇌과학', '양자물리', '기후변화')\n"
        "- 전문·심화 도서라면 학술 하위개념을 포함할 수 있으나 **최대 2개로 제한**하고 "
        "나머지는 일반 독자도 검색할 수 있는 대중어로 채우세요. "
        "(예: '신경가소성', '양자얽힘' 각 1개씩 + 대중어 3개 이상)\n"
        "- '세계관', '과학탐구', '과학적논리'처럼 너무 포괄적이거나 메타적인 표현은 제외하세요.\n"
    ),
    "기술과학": (
        "이 책은 기술과학 또는 실용 도서입니다.\n"
        "- 구체적 기법, 도구명, 실천 항목 위주로 추출하세요.\n"
        "- 예: '머신러닝', '코바늘뜨기', '비건요리', '근력운동', '제미나이', '노트북LM'\n"
        "- **[독자 수준 분기]** 제목·분류에 '입문', '기초', '쉽게', '활용' 같은 단서가 있으면 "
        "일반 독자 대상 대중어를 우선 선택하세요. (예: '파이썬기초', 'AI활용', '데이터분석')\n"
        "- 전문·심화 도서라면 학술·기술 하위개념을 포함할 수 있으나 **최대 2개로 제한**하고 "
        "나머지는 일반 이용자도 검색할 수 있는 실용어로 채우세요. "
        "(예: '비동기프로그래밍', '트랜스포머모델' 각 1개씩 + 실용어 3개 이상)\n"
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
    "에세이": "에세이",
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

# 단순 장르명 필터 — 한정어 없이 장르만 단독으로 쓰인 경우 (예: 에세이, 소설, 희곡)
# 한정어가 붙은 복합어(성장소설, 현대시, 추리소설 등)는 해당하지 않음
_PURE_GENRE_LABELS = frozenset({
    "에세이", "수필", "산문",
    "소설", "희곡",
    "동화", "만화", "웹툰",
    "시집", "소설집", "산문집", "에세이집", "단편집", "수필집",
    "그림책", "동시집", "시화집",
})

# 국가명+문학장르 복합어 필터 (예: 한국문학, 일본소설, 영미소설)
_COUNTRY_GENRE_RE = re.compile(
    r"^(한국|일본|영미|중국|프랑스|독일|러시아|미국|영국|스페인|이탈리아|북유럽)"
    r"(문학|소설|에세이|시|희곡|단편소설|장편소설|수필|동화|산문|시집|소설집|산문집|문예|시문학)$"
)

# 국가+장르만 덧붙인 형태 (예: 현대한국소설, 당대일본문학) — 이용자 검색어로는 너무 넓음
_EXTENDED_COUNTRY_GENRE_RE = re.compile(
    r"^(현대|당대|근대)?"
    r"(한국|일본|영미|중국|미국|영국|프랑스|독일|러시아|스페인|이탈리아|북유럽)"
    r"(소설|시|희곡|문학|에세이|수필|산문)$"
)

# 전 분야 공통 — 검색효용 없는 추상 접미 패턴
# '실존의미', '자기반추', '전통가치', '서정적문체' 등 이용자가 검색창에 치지 않는 메타·철학어
_LOW_VALUE_SUFFIX_RE = re.compile(
    r"(의미|이면|반추|가치관?|문체|정서|사유|고찰|성찰|탐색|조명|인식론?|존재론?)$"
)

# 에세이 전용 — 형용사적 감상어 (이용자 검색어로 쓰이지 않는 정서 수식어)
_ESSAY_SENTIMENT_RE = re.compile(
    r"^(따뜻한?|따스한?|여운|감성적?|힐링|위로|공감|소소한?|잔잔한?|잔잔함"
    r"|감동적?|아늑한?|포근한?|따뜻함|아름다운?|사색적?|서정적?|담담한?)$"
)

# 문학·서사 비평·메타 표현 — 이용자 검색 키워드로는 효용이 낮은 편
_LITERATURE_META_RE = re.compile(
    r"(문학적|비평적|미학적|서사적)(조각|형상|탐구|사유|분석|읽기)$"
    r"|^(감정|정서|내적|심리)서사$"
    r"|^(언어|담론|서사|비평)(탐구|분석|구조|전략)$"
)

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
    "kpipa키워드",
    "kpipa목차url",
    "kpipa소개url",
    "국립중앙도서관kdc",
    "핵심도구",
    "구글계정생성",
    # 평론·메타어 변형 (모델 출력 누락 대비 명시 차단)
    "문학적조각",
    "감정서사",
    "언어탐구",
    "서사구조",
    "서사전략",
    # 기능 약한 일반어 — 검색 변별력 없음
    "연구",
    "개론",
    "이론",
    "실천",
    "활동",
    "접근",
    "관점",
    "방향",
    "현황",
    "동향",
    "개요",
    "사례",
    "특징",
    "의의",
    "시사점",
    "과제",
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
    if _COUNTRY_GENRE_RE.match(compact):
        return True
    if _EXTENDED_COUNTRY_GENRE_RE.match(compact):
        return True
    if category_group in ("문학", "에세이") and _LITERATURE_META_RE.search(compact):
        return True
    if category_group == "에세이" and _ESSAY_SENTIMENT_RE.match(compact):
        return True
    if _LOW_VALUE_SUFFIX_RE.search(compact):
        return True
    # '과/와'로 두 개념을 이어 붙인 결합어 — 각 개념이 별도 키워드여야 함
    if re.search(r"[가-힣]{2,}[과와][가-힣]{2,}", compact):
        return True
    if compact in _PURE_GENRE_LABELS:
        return True
    # 한 글자 한국어: 검색 변별력 없음
    if re.fullmatch(r"[가-힣]", compact):
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
) -> tuple[str, TokenUsage | None]:
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
    usage_raw = data.get("usage") or {}
    usage: TokenUsage | None = None
    if usage_raw:
        usage = TokenUsage(
            prompt_tokens=int(usage_raw.get("prompt_tokens") or 0),
            completion_tokens=int(usage_raw.get("completion_tokens") or 0),
            total_tokens=int(usage_raw.get("total_tokens") or 0),
        )
    return (content or "").strip(), usage


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
    category_group = get_category_group(category)
    category_prompt = get_category_prompt(category)

    mode_prompt = (
        "## 653 키워드 선정 — 3단계 판단 기준\n\n"
        "### [판단 기준 1] 주제어인가, 내용어인가 ← 가장 중요\n"
        "책에 '등장'하는 사물·사례·인물·현상은 내용어입니다.\n"
        "653 키워드는 '책이 무엇을 다루는가'를 가리키는 주제어여야 합니다.\n"
        "내용어 → 주제어 치환 필수 예시:\n"
        "  음식물쓰레기·비닐봉투·플라스틱병 → 음식낭비·자원순환·플라스틱오염·환경실천\n"
        "  삼성전자·네이버·카카오(사례 기업) → 대기업전략·플랫폼경제·기술혁신·IT산업\n"
        "  아버지·어머니·형제(등장인물) → 가족관계·세대갈등·부모자식·가족돌봄\n"
        "  신호등·버스·지하철(소재 사물) → 도시교통·대중교통·도시인프라\n"
        "  ※ 예외: 기술서의 도구명(파이썬, 챗GPT, 엑셀, 노션)은 그 자체가 핵심 검색어 → 허용\n\n"
        "### [판단 기준 2] 이용자가 실제로 검색창에 입력할 단어인가\n"
        "도서관 통합검색에서 이 책을 찾는 이용자가 실제로 입력할 명사·명사구여야 합니다.\n"
        "  ✗ 판촉·감상어(따뜻한, 감동적, 힐링) — 이용자가 검색하지 않음\n"
        "  ✗ 비평·메타어(서사구조, 문학적탐구, 실존의미) — 검색 효용 없음\n"
        "  ✗ 기능 약한 일반어(연구, 개론, 방법, 이론, 활동, 실천) — 검색 변별력 없음\n"
        "  ○ 구체 주제어(번아웃, 자존감, 기후변화, 반도체산업) — 검색 효용 높음\n\n"
        "### [판단 기준 3] 추상화 수준 조정\n"
        "너무 구체적(사례·인스턴스 수준) → 한 단계 위 주제로 치환 (판단 기준 1 참조)\n"
        "너무 추상적(상위 분류 수준) → 구체 하위 개념으로 치환:\n"
        "  사회문제 → 노동문제·청년실업·주거불안·빈부격차\n"
        "  환경 → 기후변화·탄소중립·생태계파괴·미세먼지\n"
        "  문화 → 대중문화·전통문화·문화산업·문화정책\n\n"
        "### [형식·필터 규칙]\n"
        "- 형식: 2~6글자 복합명사, 붙여쓰기(공백 없음)\n"
        "- 제외: 제목·저자 유래어 (기술서 도구명은 허용)\n"
        "- 제외: '과/와/의'로 이어 붙인 결합어 → 각 개념을 분리하거나 대표어 하나로 치환\n"
        "  (예: 도망과피난 → 도망·피난 각각 / 삶의이면 → 삶·이면 분리)\n"
        "- 제외: 동의어·유사어 중복 → 대표어 1개만 (예: 신세대+젊은세대 → 청년세대)\n"
        "- 제외: 국가명+문학장르 결합어 (한국문학, 일본소설, 현대한국소설 등)\n"
        "- 제외: 한정어 없는 단순 장르명 (소설, 에세이, 수필, 희곡 등; 성장소설·추리소설은 허용)\n"
        "- 제외: 추상 접미어 결합어 끝이 의미·이면·반추·가치관·문체·정서·사유·고찰·성찰·탐색·조명·인식론·존재론인 단어\n"
        "- 문학에서 '한국소설'·'현대한국소설'류 → 성장소설·심리소설·도시소설 등으로 치환\n"
        "- 정보 부족 시: 상위 장르·국가명만으로 채우지 말고 분류 꼬리에서 구체 주제를 추론\n"
        f"- 목표: 중복 없이 최소 5개, 최대 {max_keywords}개\n"
    )

    literature_prompt = ""
    if category_group in ("문학", "에세이"):
        literature_prompt = (
            "\n[문학 그룹 전용 — 이용자 도서 검색용 소재어]\n"
            "- 문학 도서는 비평·서사이론형 메타 표현을 키워드로 쓰지 마세요. "
            "이용자가 검색창에 치기 어렵고, 작품 소재를 직접 가리키지도 않습니다.\n"
            "- 금지 꼴(예시, 이 패턴에 해당하면 출력 금지): "
            "문학적/비평적/미학적/서사적 + 조각·형상·탐구·사유·분석·읽기; "
            "감정서사·정서서사·내적서사·심리서사; "
            "언어탐구·담론탐구·서사탐구·비평탐구·서사구조·서사전략·담론분석 등\n"
            "- 대신 작품에서 읽히는 구체 소재·배경·주제·기법(예: 식민지, 성장, 가족, 전쟁, 도시, 서정, 시점, 화자, 여성, 일상 등)을 명사형으로 선택하세요.\n"
        )

    global_principles = (
        "## 653 필드 생성 4대 전역 원칙 (모든 분야 공통 적용)\n\n"
        "### 1. 변별성 (Distinctiveness): 정보의 보완과 확장\n"
        "서명(245)이나 주분류명(KDC)에 포함된 단어를 그대로 반복하지 마십시오. "
        "제목이 지시하는 방향 너머의 하위 개념이나 연관 주제를 발굴하여 데이터의 밀도를 높이십시오.\n\n"
        "### 2. 입상성 (Granularity): 추상의 안개를 걷어내는 구체성\n"
        "범위가 너무 넓은 상위 개념(예: 과학, 역사, 에세이) 대신, 도서의 핵심 실체를 드러내는 "
        "구체적인 명사(Entity)를 선택하십시오. "
        "이용자가 검색 결과에서 이 책을 정확히 솎아낼 수 있을 만큼 날카로운 단어여야 합니다.\n\n"
        "### 3. 의도성 (Search Intent): 이용자의 결핍과 상황에 응답\n"
        "감상 형용사(따뜻한, 슬픈)로 표현하지 마십시오. "
        "독자가 처한 사회적 상황, 정체성, 해결하고자 하는 문제를 키워드로 치환하십시오. "
        "(예: ‘위로’ → ‘번아웃’, ‘마음치유’ / ‘성장’ → ‘자존감’, ‘회복탄력성’)\n"
        "또한 책에 ‘등장’하는 사물·사례보다, 그 사물이 다루어지는 ‘주제 맥락’을 선택하십시오. "
        "(예: ‘음식물쓰레기’ → ‘음식낭비’ 또는 ‘자원순환’ / ‘삼성전자’ → ‘대기업전략’)\n\n"
        "### 4. 시의성 (Timeliness): 고정된 체계를 넘는 살아있는 언어\n"
        "도서관의 통제어 체계에만 갇히지 마십시오. "
        "현재 이용자들이 해당 주제를 부를 때 사용하는 가장 대중적이고 시의성 있는 용어(신조어, 외래어 포함)를 "
        "적극적으로 반영하십시오. (예: ‘거대언어모델’ 옆에 ‘LLM’ 병기)\n\n"
    )

    system_msg = {
        "role": "system",
        "content": (
            "당신은 KORMARC 작성 경험이 풍부한 도서관 메타데이터 전문가입니다.\n"
            "주어진 도서 정보를 바탕으로 MARC 653 자유주제어를 생성하세요.\n\n"
            f"{global_principles}"
            f"{mode_prompt}{literature_prompt}\n"
            f"카테고리 그룹: {category_group}\n"
            f"[카테고리별 지침]\n{category_prompt}\n"
            "출력: `$a키워드1 $a키워드2 ...` 한 줄만, 사고 과정 없이 최종 결과만\n"
            "- 상위 분류명(건강·취미 등)은 구체 하위개념으로 치환\n"
            "- 유효 키워드 부족 시에도 ‘OO문학·OO소설’ 식 넓은 장르명만으로 채우지 말 것\n\n"
            "출력 예시: ‘$a정서조절 $a성장소설’ (쉼표·번호·설명문 금지)"
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
            f"### 작업 지시 (내부적으로 3단계를 거쳐 최종 결과만 출력)\n"
            f"1단계: 이 책의 핵심 주제 영역 2~3개를 파악한다.\n"
            f"2단계: 각 주제 영역에서 이용자가 검색창에 입력할 구체 키워드를 추출하되, "
            f"'내용어(책에 등장하는 사물·사례)'인지 '주제어(책이 다루는 개념)'인지 점검하고 내용어는 주제어로 치환한다.\n"
            f"3단계: 카테고리별 지침의 필터 규칙을 적용해 최종 목록을 확정한다.\n\n"
            f"출력: `$a키워드1 $a키워드2 ...` 한 줄 (최소 5개, 최대 {max_keywords}개, 사고 과정 없이 결과만)"
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


# 카테고리에서 국가+장르가 걸려 fallback이 비어버릴 때 제공하는 장르별 대체어
_GENRE_FALLBACKS: dict[str, list[str]] = {
    "소설": ["현대소설", "장편소설"],
    "시": ["현대시", "서정시"],
    "에세이": ["에세이"],
    "희곡": ["현대희곡"],
    "동화": ["어린이문학"],
    "만화": ["그래픽노블"],
}


def _extract_category_candidates(category: str) -> list[str]:
    """분류 체인의 구체 하위 분야명을 보강 후보로 사용한다."""
    category_group = get_category_group(category)
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
        if _is_low_value_keyword(n, category_group):
            continue
        candidates.append(token)

    # 유효 후보가 없으면 (국가+장르 필터 등으로 모두 차단된 경우) 장르 대체어 사용
    # 카테고리 오른쪽(구체) → 왼쪽(일반) 순서로 장르를 탐색
    if not candidates:
        parts = [p.strip() for p in category.split(">") if p.strip()]
        for part in reversed(parts):
            part_compact = part.replace(" ", "")
            for genre, fallbacks in _GENRE_FALLBACKS.items():
                if genre in part_compact:
                    candidates.extend(fallbacks)
                    break
            if candidates:
                break

    return candidates


def finalize_653(
    ai_output: str,
    forbidden_set: set[str],
    max_keywords: int = 7,
    min_keywords: int = 5,
    category: str = "",
    toc: str = "",
    description: str = "",
) -> tuple[str, Field653Quality]:
    """AI 출력에서 금지어·저효용어를 제거하고 $a 형식과 품질 지표를 함께 반환."""
    keywords = [k.strip() for k in ai_output.split("$a") if k.strip()]
    ai_raw_count = len(keywords)

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

    ai_valid_count = len(valid_keywords)
    filtered_count = ai_raw_count - ai_valid_count
    backup_used = ai_valid_count == 0

    if backup_used:
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

    category_fallback_used = len(valid_keywords) < min_keywords
    if category_fallback_used:
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

    final_count = len(valid_keywords[:max_keywords])

    # 품질 점수 산출
    flags: list[str] = []
    if ai_raw_count < 3:
        flags.append("AI생성부족")
    filter_rate = filtered_count / ai_raw_count if ai_raw_count > 0 else 0.0
    if filter_rate > 0.5:
        flags.append("과다차단")
    if backup_used:
        flags.append("텍스트fallback사용")
    if category_fallback_used:
        flags.append("카테고리fallback사용")
    if final_count < min_keywords:
        flags.append("키워드부족")

    score = min(final_count, max_keywords) / max(max_keywords, 1)
    if filter_rate > 0.3:
        score -= (filter_rate - 0.3) * 0.5
    if backup_used:
        score -= 0.15
    if category_fallback_used:
        score -= 0.10
    score = round(max(0.0, min(1.0, score)), 3)

    quality = Field653Quality(
        ai_raw_count=ai_raw_count,
        filtered_count=filtered_count,
        final_count=final_count,
        backup_used=backup_used,
        category_fallback_used=category_fallback_used,
        quality_score=score,
        flags=flags,
    )
    return "".join([f"$a{k}" for k in valid_keywords[:max_keywords]]), quality


async def generate_653_subfield_line(
    meta: AladinMetadata653,
    max_keywords: int = 7,
    min_keywords: int = 5,
    settings: Settings | None = None,
    client: httpx.AsyncClient | None = None,
) -> tuple[str | None, str | None, TokenUsage | None, Field653Quality | None]:
    """
    Returns (subfield_line, error, token_usage, quality).
    subfield_line: '$a키워드1$a키워드2…' 형식 (=653 접두 없음), 실패 시 None.
    quality: Field653Quality 품질 지표, 실패 시 None.
    """
    s = get_settings() if settings is None else settings
    if not s.openai_api_key:
        return None, "OPENAI_API_KEY가 설정되지 않았습니다.", None, None

    category = meta.category
    title = meta.title
    authors = clean_author_str(meta.authors)
    description = meta.description
    toc = meta.toc

    sys_m, user_m = _system_and_user_messages(
        category, title, authors, description, toc, max_keywords
    )
    try:
        raw, usage = await _openai_chat_completions(
            s.openai_api_key,
            s.openai_base_url,
            s.openai_model,
            [sys_m, user_m],
            settings=s,
            client=client,
            temperature=0.2,
            max_tokens=200,
            timeout=60.0,
        )
    except Exception as e:
        logger.exception("OpenAI 653 호출 실패")
        return None, str(e), None, None

    forbidden = build_forbidden_set(title, authors)
    kws = parse_keyword_line(raw)
    ai_output = "".join(f"$a{kw}" for kw in kws if should_keep_keyword(kw, forbidden))
    subfield_line, quality = finalize_653(
        ai_output,
        forbidden,
        max_keywords=max_keywords,
        min_keywords=min_keywords,
        category=category,
        toc=toc,
        description=description,
    )
    if not subfield_line:
        return None, "유효한 키워드를 추출하지 못했습니다.", usage, quality

    return subfield_line, None, usage, quality


def build_marc_653_line(subfield_line: str) -> str:
    """'$a..$a..' → MRK 한 줄(1215_main `=653  \\\\` + 서브필드 꼴)."""
    compact = subfield_line.replace(" ", "")
    return f"=653  \\\\{compact}"
