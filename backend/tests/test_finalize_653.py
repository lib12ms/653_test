"""finalize_653 — fallback 로직 검증."""
import pytest
from app.ai_service import finalize_653


# ── 문학 fallback 검증 ────────────────────────────────────────────────────────

def test_literature_skips_text_backup():
    """문학: AI 결과 0개 → 텍스트 fallback 토큰(주인공·서울·이야기)이 삽입되지 않아야 한다."""
    bad_tokens_description = "주인공 서울 이야기 비밀 복수 기억상실 연인"
    result, quality = finalize_653(
        ai_output="",
        forbidden_set=set(),
        max_keywords=7,
        min_keywords=3,
        category="국내도서 > 소설/시/희곡 > 한국소설 > 현대소설",
        toc="",
        description=bad_tokens_description,
    )
    kws = [k for k in result.split("$a") if k]
    assert "주인공" not in kws
    assert "서울" not in kws
    assert "이야기" not in kws
    assert quality.backup_used is True


def test_literature_category_fallback_provides_genre():
    """문학: 텍스트 fallback 건너뛴 뒤 카테고리 fallback이 장르명을 제공해야 한다."""
    result, quality = finalize_653(
        ai_output="",
        forbidden_set=set(),
        max_keywords=7,
        min_keywords=3,
        category="국내도서 > 소설/시/희곡 > 한국소설 > 현대소설",
        toc="",
        description="",
    )
    kws = [k for k in result.split("$a") if k]
    # 카테고리 fallback → 현대소설 (장르명, 유형A) 포함
    assert "현대소설" in kws
    # 유통 분류어 금지
    assert "국내도서" not in kws


def test_literature_category_deny_terms():
    """CATEGORY_CANDIDATE_DENY에 추가된 유통 분류어가 카테고리 fallback에 등장하지 않아야 한다."""
    result, quality = finalize_653(
        ai_output="",
        forbidden_set=set(),
        max_keywords=7,
        min_keywords=1,
        category="국내도서 > 소설/시/희곡",
        toc="",
        description="",
    )
    kws = [k for k in result.split("$a") if k]
    assert "국내도서" not in kws
    assert "외국도서" not in kws


# ── 비문학(에세이) — 텍스트 fallback은 그대로 동작해야 함 ─────────────────────

def test_non_literature_text_backup_still_works():
    """에세이: AI 결과 0개이면 텍스트 토큰이 fallback으로 사용되어야 한다."""
    result, quality = finalize_653(
        ai_output="",
        forbidden_set=set(),
        max_keywords=7,
        min_keywords=3,
        category="국내도서 > 에세이 > 한국에세이",
        toc="제주살이 고양이 골목 독립 여행",
        description="제주에서 혼자 살며 쓴 에세이",
    )
    assert result != ""
    assert quality.backup_used is True
