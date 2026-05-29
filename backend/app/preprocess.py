"""653 공통 전처리(1215_main._norm, _clean_author_str, 금지어)."""
import re
import unicodedata

TITLE_DERIVED_ALLOWED_KEYWORDS = {
    "ai글쓰기",
    "생성형ai",
    "저작권",
    "창작윤리",
    "콘텐츠창작",
    "제미나이",
    "구글ai",
    "구글워크스페이스",
    "노트북lm",
    "딥리서치",
    "ai코딩",
    "인공지능도구",
}


def norm_text(text: str) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"[^\w\s\uac00-\ud7a3]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def clean_author_str(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"[/;·,]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def extract_toc_major(toc: str, max_chars: int = 300) -> str:
    """
    목차에서 장(章) 단위 제목만 추출.
    N장·N부·N편·Part N·Chapter N 패턴 매칭.
    에필로그·참고문헌·부록 등 후미 항목 제거.
    """
    DENY_KEYWORDS = {
        "에필로그", "참고문헌", "부록", "찾아보기",
        "색인", "저자소개", "감사의글", "옮긴이",
        "프롤로그", "머리말", "들어가며",
    }

    lines = toc.replace("·", "\n").splitlines()
    major = []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        if any(d in line for d in DENY_KEYWORDS):
            continue
        if re.match(
            r"^(제?\s*\d+\s*[장부편권화]\s|"
            r"part\s*\d+|chapter\s*\d+)",
            line, re.IGNORECASE,
        ):
            major.append(line)

    result = " / ".join(major)

    # 장 단위 추출 결과 없으면 원본 앞부분 사용
    if not result:
        result = toc

    return result[:max_chars]


def clean_toc_for_ai(toc_text: str) -> str:
    """목차 문자열에서 페이지 번호·불용어를 제거하고 장 단위로 압축해 LLM 입력 품질을 높인다."""
    if not toc_text:
        return ""

    # 1) 페이지 번호 연결부 제거 (예: "서론 ...... 15")
    text = re.sub(r"(\.{2,}|…|[-_]{2,})\s*\d+", " ", toc_text)

    # 2) 불용어 제거 (장/부 표기는 extract_toc_major 감지에 사용하므로 유지)
    stop_words = ["목차", "차례", "CONTENTS"]
    for word in stop_words:
        text = text.replace(word, " ")

    cleaned = re.sub(r"\s+", " ", text).strip()
    return extract_toc_major(cleaned, max_chars=300)


def clean_description_for_ai(description_text: str) -> str:
    """
    저자 이력/수상/출간 정보 위주의 문장을 제거해
    주제 중심 설명만 LLM 입력으로 남긴다.
    """
    if not description_text:
        return ""

    text = description_text.replace("\r", "\n")
    lines = [ln.strip() for ln in re.split(r"[\n]+", text) if ln.strip()]

    bio_patterns = [
        r"\b(19|20)\d{2}\s*년\b",
        r"\b(작가|저자|역자|엮은이|지은이)\b",
        r"\b(등단|수상|출간|데뷔|연재|활동)\b",
        r"\b(장편소설|소설집|시집|산문집|평론집)\b",
        r"\b(문학상|대상|우수상|신인상)\b",
    ]
    bio_re = re.compile("|".join(bio_patterns), flags=re.I)

    kept: list[str] = []
    for ln in lines:
        # 너무 짧은 이력성 라인 제거
        if len(ln) <= 6 and bio_re.search(ln):
            continue
        if bio_re.search(ln):
            continue
        kept.append(ln)

    if not kept:
        # 전부 제거될 경우 원문 축약본을 fallback으로 사용
        fallback = re.sub(r"\s+", " ", description_text).strip()
        return fallback[:1200]

    out = re.sub(r"\s+", " ", " ".join(kept)).strip()
    return out[:1200]


def clean_category_for_ai(category_str: str, remove_words: list[str] | None = None) -> str:
    """
    알라딘 카테고리에서 유통성 분류어를 제거한다.
    예: "국내도서 > 실용서 > 요리 > 한식" -> "요리 > 한식"
    """
    if not category_str:
        return ""

    default_words = [
        "국내도서", "외국도서", "실용서", "단행본", "ebook", "e-book", "전자책",
        "베스트셀러", "신간", "스테디셀러", "md추천",
    ]
    words = remove_words or default_words
    remove_exact = {norm_text(w) for w in words}
    remove_contains = tuple(norm_text(w) for w in words)
    parts = [p.strip() for p in category_str.split(">") if p.strip()]
    cleaned: list[str] = []
    for p in parts:
        n = norm_text(p)
        if n in remove_exact:
            continue
        # 변형 표기 대응 (예: "국내도서(아동)", "외국도서-원서", "신간추천")
        if any(tok in n for tok in remove_contains):
            continue
        # 유통성 접두/접미를 포함한 짧은 레이블 제거
        if ("도서" in n or "판매" in n) and len(n) <= 8:
            continue
        cleaned.append(p)
    # 중복 토막 제거
    deduped: list[str] = []
    seen: set[str] = set()
    for c in cleaned:
        key = norm_text(c)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(c)
    return " > ".join(deduped)


def build_forbidden_set(title: str, authors: str) -> set[str]:
    t_norm = norm_text(title)
    a_norm = norm_text(authors)
    forb: set[str] = set()
    if t_norm:
        forb.update(t_norm.split())
        forb.add(t_norm.replace(" ", ""))
    if a_norm:
        forb.update(a_norm.split())
        forb.add(a_norm.replace(" ", ""))
    return {f for f in forb if f and len(f) >= 2}


def should_keep_keyword(kw: str, forbidden: set[str]) -> bool:
    n = norm_text(kw)
    compact = n.replace(" ", "")
    if not n or len(compact) < 2:
        return False
    if compact in TITLE_DERIVED_ALLOWED_KEYWORDS:
        return True
    for tok in forbidden:
        tok_compact = tok.replace(" ", "")
        if compact == tok_compact or compact in tok_compact:
            return False
        if len(tok_compact) >= 3 and tok_compact in compact:
            return False
    return True


def validate_keyword(keyword: str, forbidden_set: set[str]) -> bool:
    """653 후보 키워드 검수용 별칭 함수."""
    return should_keep_keyword(keyword, forbidden_set)
