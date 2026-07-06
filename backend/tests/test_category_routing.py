"""카테고리 라우팅 규칙 검증 — get_category_group / _category_group_from_text."""
import pytest
from app.ai_service import get_category_group


# ── 인문학 하위분야 세분화 ──────────────────────────────────────────────────────

def test_history_in_humanities_routes_to_history():
    """인문학 > 역사/문화 — '역사' 키가 '인문학' 키보다 우선해야 한다."""
    assert get_category_group("인문학 > 역사/문화") == "역사"


def test_psychology_in_humanities_routes_to_psychology():
    """인문학 > 심리학 — '심리학' 키가 '인문학' 키보다 우선해야 한다."""
    assert get_category_group("인문학 > 심리학 일반") == "심리학"


def test_philosophy_in_humanities_routes_to_philosophy():
    """인문학 > 철학 — '철학' 키가 '인문학' 키보다 우선해야 한다."""
    assert get_category_group("인문학 > 철학 일반 > 교양 철학") == "철학"


def test_plain_humanities_routes_to_inmunhak():
    """인문학 > 글쓰기 — 심리/철학/역사 키 없으면 '인문학' 그룹."""
    assert get_category_group("인문학 > 책읽기/글쓰기") == "인문학"


def test_humanities_essay_routes_to_inmunhak():
    """인문학 > 인문 에세이 — '에세이' 키보다 '인문학' 블록이 먼저 적용되어야 한다."""
    # '에세이'가 CATEGORY_MAP에도 있으므로 순서 중요
    result = get_category_group("인문학 > 인문 에세이")
    # 인문학 블록에서 심리/철학/역사 없음 → 인문학 그룹
    assert result == "인문학"


# ── 사회과학 하위분야 ─────────────────────────────────────────────────────────

def test_social_science_education_routes_to_education():
    """사회과학 > 교육학 — 교육 그룹으로 라우팅."""
    assert get_category_group("사회과학 > 교육학") == "교육"


def test_economics_routes_to_economics():
    """경제경영 카테고리 — 경제경영 그룹."""
    assert get_category_group("경제경영 > 경영 일반") == "경제경영"


# ── 예술 하위분야 — 건축 분기 ─────────────────────────────────────────────────

def test_art_architecture_routes_to_practical():
    """예술/대중문화 > 건축 — 생활실용 그룹(기술과학 아님)."""
    assert get_category_group("예술/대중문화 > 건축") == "생활실용"


def test_art_music_routes_to_art():
    """예술/대중문화 > 음악 — 예술 그룹."""
    assert get_category_group("예술/대중문화 > 음악 > 클래식") == "예술"


# ── IT 라우팅 — 모바일 키 제거 후에도 '컴퓨터' 키로 정상 매칭 ─────────────────

def test_computer_mobile_routes_to_it():
    """컴퓨터/모바일 — '모바일' 키 제거 후에도 '컴퓨터' 키로 IT컴퓨터 라우팅."""
    assert get_category_group("컴퓨터/모바일 > 프로그래밍 언어") == "IT컴퓨터"


def test_computer_alone_routes_to_it():
    """컴퓨터 단독 카테고리 — IT컴퓨터 그룹."""
    assert get_category_group("컴퓨터 > 운영체제") == "IT컴퓨터"


# ── KDC content_code 보정 ─────────────────────────────────────────────────────

def test_kdc_override_from_other_to_literature():
    """'기타'로 라우팅된 경우 KDC 코드(문학 강)로 보정."""
    assert get_category_group("전집/세트", content_code="810") == "문학"


def test_kdc_override_from_inmunhak_to_religion():
    """'인문학'으로 라우팅된 경우 KDC 코드(종교 강)로 보정."""
    assert get_category_group("인문학 > 기타", content_code="220") == "종교/역학"


def test_kdc_no_override_for_specific_group():
    """명확한 그룹(문학)은 KDC 코드가 있어도 덮어쓰지 않는다."""
    assert get_category_group("소설/시/희곡 > 한국소설", content_code="400") == "문학"


def test_kdc_empty_content_code_no_change():
    """content_code가 빈 문자열이면 보정 없음."""
    assert get_category_group("인문학 > 기타", content_code="") == "인문학"
