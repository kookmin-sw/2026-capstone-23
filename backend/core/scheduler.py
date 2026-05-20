"""
자동 실행 스케줄러: 지정 시간에 data/inputs/ 미처리 파일 자동 변환
- 시간 스케줄 기반 자동 실행
- API 트리거 즉시 실행
"""
import time
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, List

from core.config import SUPPORTED_EXTENSIONS, EXCLUDE_FILES
from core.pipeline import DocumentPipeline
from core.batch_state import (
    load_batch_state,
    save_batch_state,
    is_stop_requested,
    set_stop_requested,
    print_batch_completion_stats,
    remove_completed_input,
)
from storage.sqlite_kv import load_json_entry, save_json_entry


# ── Schedule Config 관리 ──────────────────────────────────────────

DEFAULT_SCHEDULE = {
    "enabled": False,
    "schedule_mode": "always",      # "always" | "timed"
    "start_time": "00:00",          # HH:MM
    "end_time": "23:59",            # HH:MM
    "language": "한국어",
    "vlm_model": "openrouter/qwen3-vl-32b",
    "parallel": 1,
    "max_retries": 1,               # 실패 시 재시도 횟수 (0~3)
    "poll_interval_seconds": 30,
}
def _get_schedule_key(config) -> str:
    return str(config.tmp_root.resolve())


def load_schedule_config(config) -> Dict[str, Any]:
    try:
        data = load_json_entry("schedule_config", _get_schedule_key(config))
        if not isinstance(data, dict):
            return dict(DEFAULT_SCHEDULE)
        # 누락된 키는 기본값으로 채움
        merged = dict(DEFAULT_SCHEDULE)
        merged.update(data)
        return merged
    except Exception as e:
        print(f"[scheduler] 스케줄 설정 로드 실패: {e}")
        return dict(DEFAULT_SCHEDULE)


def save_schedule_config(config, schedule: Dict[str, Any]) -> None:
    payload = dict(schedule)
    payload["updated_at"] = datetime.now().isoformat()
    save_json_entry("schedule_config", _get_schedule_key(config), payload)


# ── 시간 윈도우 확인 ──────────────────────────────────────────────

def is_within_time_window(schedule: Dict[str, Any]) -> bool:
    """현재 시간이 스케줄 허용 구간 내인지 확인."""
    if schedule.get("schedule_mode") == "always":
        return True

    now = datetime.now()
    now_minutes = now.hour * 60 + now.minute

    try:
        sh, sm = map(int, schedule.get("start_time", "00:00").split(":"))
        eh, em = map(int, schedule.get("end_time", "23:59").split(":"))
    except (ValueError, AttributeError):
        return True  # 파싱 실패 시 항상 허용

    start_minutes = sh * 60 + sm
    end_minutes = eh * 60 + em

    if start_minutes <= end_minutes:
        # 일반 구간 (예: 09:00 ~ 18:00)
        return start_minutes <= now_minutes <= end_minutes
    else:
        # 자정 넘김 (예: 22:00 ~ 06:00)
        return now_minutes >= start_minutes or now_minutes <= end_minutes


# ── 미처리 파일 스캔 ─────────────────────────────────────────────

def scan_unprocessed_files(config) -> List[Path]:
    """data/inputs/에서 대응 output이 없는 파일 목록 반환."""
    input_root = config.input_root.resolve()
    output_root = config.output_root.resolve()

    if not input_root.exists():
        return []

    unprocessed = []
    for file_path in input_root.rglob("*"):
        if not file_path.is_file():
            continue
        if file_path.name.startswith('.'):
            continue
        if file_path.name in EXCLUDE_FILES:
            continue
        if file_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue

        # 대응 output 파일 확인
        try:
            rel = file_path.relative_to(input_root)
        except ValueError:
            continue
        output_path = output_root / rel.parent / f"{file_path.stem}.txt"
        if not output_path.exists():
            unprocessed.append(file_path)

    return sorted(unprocessed)


# ── AutoProcessor ────────────────────────────────────────────────

class AutoProcessor:
    """백그라운드 자동 처리기: 스케줄/API 기반으로 미처리 파일 변환."""

    def __init__(self, config):
        self.config = config
        self._pipeline: Optional[DocumentPipeline] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._processing = False  # 현재 배치 처리 중 여부
        self._lock = threading.Lock()

    def _ensure_pipeline(self, vlm_model: str) -> DocumentPipeline:
        if self._pipeline is None:
            self._pipeline = DocumentPipeline(self.config)
        self._pipeline.update_vlm_model(vlm_model)
        return self._pipeline

    def start(self) -> bool:
        """스케줄러 루프 시작. 이미 실행 중이면 False."""
        with self._lock:
            if self._running:
                return False
            self._running = True
        t = threading.Thread(target=self._loop, daemon=True, name="auto-processor")
        self._thread = t
        t.start()
        print("[scheduler] 자동 처리기 시작됨")
        return True

    def stop(self) -> None:
        """스케줄러 루프 중지."""
        self._running = False
        if self._processing:
            set_stop_requested(self.config)
        print("[scheduler] 자동 처리기 중지 요청")

    def trigger_now(self, language: str, vlm_model: str, parallel: int = 1, max_retries: int = 1) -> int:
        """스케줄 무시하고 즉시 실행 (API용). 미처리 파일 수 반환. -1이면 이미 실행 중."""
        with self._lock:
            if self._processing:
                return -1

        unprocessed = scan_unprocessed_files(self.config)
        if not unprocessed:
            return 0

        # 별도 스레드에서 처리 시작
        t = threading.Thread(
            target=self._process_batch,
            args=(unprocessed, language, vlm_model, parallel, "api"),
            kwargs={"max_retries": max_retries},
            daemon=True,
            name="auto-processor-trigger",
        )
        t.start()
        return len(unprocessed)

    def get_status(self) -> Dict[str, Any]:
        """현재 상태 반환."""
        schedule = load_schedule_config(self.config)
        batch = load_batch_state(self.config)

        status = {
            "scheduler_running": self._running,
            "processing": self._processing,
            "schedule": schedule,
            "within_time_window": is_within_time_window(schedule),
        }
        if batch:
            status["batch_status"] = batch.get("status", "idle")
            status["batch_completed"] = batch.get("completed_count", 0)
            status["batch_total"] = len(batch.get("all_items", []))
            status["batch_source"] = batch.get("source", "unknown")
        else:
            status["batch_status"] = "idle"

        return status

    # ── 내부 메서드 ──

    def _loop(self) -> None:
        """메인 스케줄러 루프."""
        while self._running:
            poll_interval = 30
            try:
                schedule = load_schedule_config(self.config)
                poll_interval = schedule.get("poll_interval_seconds", 30)

                if not schedule.get("enabled", False):
                    time.sleep(poll_interval)
                    continue

                if not is_within_time_window(schedule):
                    time.sleep(poll_interval)
                    continue

                # 기존 배치가 실행 중이면 대기
                batch = load_batch_state(self.config)
                if batch and batch.get("status") == "running":
                    time.sleep(poll_interval)
                    continue

                # 현재 처리 중이면 대기
                if self._processing:
                    time.sleep(poll_interval)
                    continue

                # 미처리 파일 스캔
                unprocessed = scan_unprocessed_files(self.config)
                if not unprocessed:
                    time.sleep(poll_interval)
                    continue

                print(f"[scheduler] 미처리 파일 {len(unprocessed)}개 발견, 변환 시작")
                self._process_batch(
                    unprocessed,
                    schedule.get("language", "한국어"),
                    schedule.get("vlm_model", "openrouter/qwen3-vl-32b"),
                    schedule.get("parallel", 1),
                    "scheduler",
                    max_retries=schedule.get("max_retries", 1),
                )

            except Exception as e:
                print(f"[scheduler] 루프 오류: {e}")
                traceback.print_exc()

            time.sleep(poll_interval)

    def _process_batch(
        self,
        files: List[Path],
        language: str,
        vlm_model: str,
        parallel: int,
        source: str,
        max_retries: int = 1,
    ) -> None:
        """파일 목록을 배치 처리."""
        self._processing = True
        start_time = time.time()

        try:
            pipeline = self._ensure_pipeline(vlm_model)
            all_items = [{"path": str(f.resolve()), "is_dir": False} for f in files]

            # 배치 상태 생성
            state = {
                "all_items": all_items,
                "completed_count": 0,
                "failed_count": 0,
                "language": language,
                "vlm_model": vlm_model,
                "parallel": parallel,
                "max_retries": max_retries,
                "status": "running",
                "stop_requested": False,
                "source": source,
                "created_at": datetime.now().isoformat(),
            }
            save_batch_state(self.config, state)

            outputs = []
            failed_count = 0

            for i, item in enumerate(all_items):
                # 중지 요청 확인
                if is_stop_requested(self.config):
                    print(f"[scheduler] 중지 요청으로 배치 중단 ({i}/{len(all_items)})")
                    break

                # 시간 윈도우 확인 (API 트리거는 무시)
                if source == "scheduler":
                    schedule = load_schedule_config(self.config)
                    if not is_within_time_window(schedule):
                        print(f"[scheduler] 시간 윈도우 종료, 배치 중단 ({i}/{len(all_items)})")
                        break

                # 스케줄러 루프 중지 확인
                if source == "scheduler" and not self._running:
                    print(f"[scheduler] 스케줄러 중지됨, 배치 중단 ({i}/{len(all_items)})")
                    break

                src = Path(item["path"])
                if not src.exists():
                    print(f"[scheduler] 파일 없음, 건너뜀: {src}")
                    failed_count += 1
                    continue

                # 처리 시도 (max_retries 횟수만큼 재시도)
                success = False
                total_attempts = max_retries + 1
                for attempt in range(total_attempts):
                    try:
                        out = pipeline.process_file(src, language=language)
                        outputs.append(str(Path(out).resolve()))
                        success = True
                        break
                    except Exception as e:
                        print(f"[scheduler] 처리 실패 {src.name} (시도 {attempt + 1}/{total_attempts}): {e}")
                        if attempt < max_retries:
                            time.sleep(30)

                if success:
                    remove_completed_input(src, self.config)
                else:
                    failed_count += 1

                # 배치 상태 업데이트
                state = load_batch_state(self.config) or {}
                state["completed_count"] = i + 1
                state["failed_count"] = failed_count
                state["last_outputs"] = outputs
                state["status"] = "running"
                save_batch_state(self.config, state)
                print(f"[scheduler] 진행: {i + 1}/{len(all_items)} ({src.name})")

            # 완료 처리
            elapsed = time.time() - start_time
            state = load_batch_state(self.config) or {}
            state["status"] = "idle"
            state["stop_requested"] = False
            state["completed_at"] = datetime.now().isoformat()
            state["total_elapsed_seconds"] = elapsed
            save_batch_state(self.config, state)
            print_batch_completion_stats(state, elapsed)

        except Exception as e:
            print(f"[scheduler] 배치 처리 오류: {e}")
            traceback.print_exc()
            # 상태를 idle로 복구
            try:
                state = load_batch_state(self.config) or {}
                state["status"] = "idle"
                state["stop_requested"] = False
                save_batch_state(self.config, state)
            except Exception:
                pass
        finally:
            self._processing = False
