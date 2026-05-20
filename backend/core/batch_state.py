"""
배치 변환 상태 저장/로드 (앱 재시작 후 이어서 실행·백그라운드 워커용)
"""
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from storage.sqlite_kv import load_json_entry, save_json_entry


def _batch_state_key(config) -> str:
    return str(config.tmp_root.resolve())


def load_batch_state(config) -> Optional[Dict[str, Any]]:
    """배치 상태 로드"""
    try:
        data = load_json_entry("batch_state", _batch_state_key(config))
        return data if isinstance(data, dict) else None
    except Exception as e:
        print(f"[WARNING] 배치 상태 로드 실패: {e}")
        return None


def save_batch_state(config, state: Dict[str, Any]) -> None:
    """배치 상태 저장"""
    payload = dict(state)
    payload["updated_at"] = datetime.now().isoformat()
    save_json_entry("batch_state", _batch_state_key(config), payload)


def set_stop_requested(config) -> None:
    """중지 요청 플래그 설정 (현재 파일 처리 후 중지)"""
    state = load_batch_state(config) or {}
    state["stop_requested"] = True
    state["updated_at"] = datetime.now().isoformat()
    save_json_entry("batch_state", _batch_state_key(config), state)
    print("[INFO] 배치 중지 요청이 설정되었습니다. 현재 파일 처리 후 중지됩니다.")


def is_stop_requested(config) -> bool:
    """중지 요청 여부 확인"""
    state = load_batch_state(config)
    return bool(state and state.get("stop_requested"))


def remove_completed_input(src_path, config) -> bool:
    """변환 성공한 원본 파일을 data/inputs에서 삭제.
    src_path가 input_root 하위에 있을 때만 삭제한다."""
    try:
        src = Path(src_path).resolve()
        input_root = config.input_root.resolve()
        if not src.exists():
            return False
        if not str(src).startswith(str(input_root)):
            return False
        src.unlink()
        print(f"[INFO] 변환 완료 원본 삭제: {src.name}")
        return True
    except Exception as e:
        print(f"[WARNING] 원본 파일 삭제 실패: {src_path} ({e})")
        return False


def print_batch_completion_stats(state: Dict[str, Any], elapsed_seconds: float) -> None:
    """배치 완료 시 터미널에 완료 통계와 총 수행시간 출력"""
    total = len(state.get("all_items", []))
    completed = int(state.get("completed_count", 0))
    failed = int(state.get("failed_count", 0))
    success = max(0, completed - failed)
    remaining = max(0, total - completed)
    minutes = int(elapsed_seconds // 60)
    secs = elapsed_seconds % 60
    time_str = f"{minutes}분 {secs:.1f}초" if minutes > 0 else f"{secs:.1f}초"
    print("")
    print("========== 배치 변환 완료 ==========")
    print("  [통계]")
    print("    총 요청 수   : {}".format(total))
    print("    완료         : {} (성공 {} / 실패 {})".format(completed, success, failed))
    print("    미처리(남음) : {}".format(remaining))
    print("  [총 수행시간]  : {}".format(time_str))
    print("====================================")
