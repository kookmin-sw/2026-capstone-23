"""처리 속도 지표"""


def compute_speed(processing_time_sec: float, page_count: int) -> dict:
    """
    Returns:
        {
          "total_sec": float,
          "sec_per_page": float,
        }
    """
    pages = max(page_count, 1)
    return {
        "total_sec": round(processing_time_sec, 2),
        "sec_per_page": round(processing_time_sec / pages, 2),
    }
