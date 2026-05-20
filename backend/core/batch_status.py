"""
배치 변환 진행 상황 확인 (터미널에서 실행)
사용: python -m core.batch_status
"""
import sys
from pathlib import Path
from datetime import datetime

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from core.config import load_config
from core.batch_state import load_batch_state


def _parse_iso(s):
    """ISO 형식 문자열을 datetime으로 파싱. 실패 시 None."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _format_duration(seconds: float) -> str:
    """초 단위를 'X분 Y초' 문자열로."""
    if seconds < 0:
        seconds = 0
    minutes = int(seconds // 60)
    secs = seconds % 60
    if minutes > 0:
        return "{}분 {:.1f}초".format(minutes, secs)
    return "{:.1f}초".format(secs)


def main():
    config = load_config()
    state = load_batch_state(config)
    if not state:
        print("배치 상태 없음 (진행 중인 배치가 없거나 batch_state.json이 없습니다.)")
        print("  상태 파일 경로: {}".format(config.tmp_root.resolve() / "batch_state.json"))
        return

    total = len(state.get("all_items", []))
    completed = int(state.get("completed_count", 0))
    failed = int(state.get("failed_count", 0))
    status = state.get("status", "?")
    stop_requested = state.get("stop_requested", False)
    created_at = _parse_iso(state.get("created_at"))
    completed_at = _parse_iso(state.get("completed_at"))
    total_elapsed_seconds = state.get("total_elapsed_seconds")

    remaining = max(0, total - completed)
    success = max(0, completed - failed)

    print("=== 배치 변환 진행 상황 ===")
    print("  총 요청 수     : {}".format(total))
    print("  완료(처리됨)   : {} (성공 {} / 실패·중단 {})".format(completed, success, failed))
    print("  미처리(남음)   : {}".format(remaining))
    print("  상태           : {}".format(status))
    if stop_requested:
        print("  중지 요청     : 예 (현재 파일 처리 후 중단 예정)")
    # 시간: 완료 시 총 수행시간, running 시 현재까지 진행 시간
    if status == "idle" and (total_elapsed_seconds is not None or (created_at and completed_at)):
        if total_elapsed_seconds is not None:
            print("  총 수행시간   : {}".format(_format_duration(total_elapsed_seconds)))
        elif created_at and completed_at:
            print("  총 수행시간   : {}".format(_format_duration((completed_at - created_at).total_seconds())))
    elif status == "running" and created_at:
        try:
            now = datetime.now()
            if created_at.tzinfo is not None and now.tzinfo is None:
                from datetime import timezone
                now = now.replace(tzinfo=timezone.utc)
            elif created_at.tzinfo is None and now.tzinfo is not None:
                now = now.replace(tzinfo=None)
            elapsed = (now - created_at).total_seconds()
            print("  현재까지 진행 시간 : {}".format(_format_duration(elapsed)))
        except Exception:
            pass
    print("==========================")


if __name__ == "__main__":
    main()
