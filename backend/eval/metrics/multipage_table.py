"""다중 페이지 표 병합 여부 확인"""
from typing import List, Optional


def compute_multipage_table(tables: List[str], has_multipage_table: bool) -> Optional[bool]:
    """
    GT에 has_multipage_table=True 로 표시된 경우,
    추출된 표가 하나의 <table>로 병합되었는지 확인.

    Returns:
        True  — 올바르게 병합됨 (표 1개)
        False — 미병합 (표 2개 이상이거나 표가 없음)
        None  — GT에서 다중 페이지 표 없다고 표시됨 (평가 skip)
    """
    if not has_multipage_table:
        return None

    if not tables:
        return False

    # 표가 정확히 1개면 병합된 것으로 간주
    return len(tables) == 1
