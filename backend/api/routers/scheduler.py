import re

from fastapi import APIRouter
from pydantic import BaseModel, Field, field_validator

from api.common import fail, ok
from api.dependencies import get_auto_processor, get_config
from core.scheduler import load_schedule_config, save_schedule_config, scan_unprocessed_files


router = APIRouter(prefix="/scheduler", tags=["scheduler"])


_TIME_PATTERN = re.compile(r"^\d{2}:\d{2}$")


class SchedulerConfigRequest(BaseModel):
    enabled: bool = False
    scheduleMode: str = Field(default="always", pattern="^(always|timed)$")
    startTime: str = Field(default="00:00")
    endTime: str = Field(default="23:59")
    language: str = Field(default="한국어", min_length=1, max_length=40)
    vlmModel: str = Field(default="openrouter/qwen3-vl-32b", min_length=1, max_length=120)
    parallel: int = Field(default=1, ge=1, le=16)
    maxRetries: int = Field(default=1, ge=0, le=3)
    pollIntervalSeconds: int = Field(default=30, ge=5, le=3600)

    @field_validator("startTime", "endTime")
    @classmethod
    def validate_time(cls, value: str) -> str:
        if not _TIME_PATTERN.match(value):
            raise ValueError("time must follow HH:MM")
        hour, minute = map(int, value.split(":"))
        if hour > 23 or minute > 59:
            raise ValueError("time must follow HH:MM")
        return value


class SchedulerTriggerRequest(BaseModel):
    language: str = Field(default="한국어", min_length=1, max_length=40)
    vlmModel: str = Field(default="openrouter/qwen3-vl-32b", min_length=1, max_length=120)
    parallel: int = Field(default=1, ge=1, le=16)
    maxRetries: int = Field(default=1, ge=0, le=3)


def _map_schedule(schedule: dict) -> dict:
    return {
        "enabled": bool(schedule.get("enabled", False)),
        "scheduleMode": schedule.get("schedule_mode", "always"),
        "startTime": schedule.get("start_time", "00:00"),
        "endTime": schedule.get("end_time", "23:59"),
        "language": schedule.get("language", "한국어"),
        "vlmModel": schedule.get("vlm_model", "openrouter/qwen3-vl-32b"),
        "parallel": int(schedule.get("parallel", 1)),
        "maxRetries": int(schedule.get("max_retries", 1)),
        "pollIntervalSeconds": int(schedule.get("poll_interval_seconds", 30)),
        "updatedAt": schedule.get("updated_at"),
    }


@router.get(
    "/config",
    summary="스케줄러 설정 조회",
    description="자동 처리 스케줄 설정값을 반환합니다. 스케줄러 설정 화면 초기 로딩에 사용합니다.",
)
def get_scheduler_config():
    schedule = load_schedule_config(get_config())
    return ok(_map_schedule(schedule))


@router.put(
    "/config",
    summary="스케줄러 설정 변경",
    description="자동 처리 활성화 여부, 시간대, 언어, 모델, 병렬도 등의 스케줄러 설정을 저장합니다.",
)
def update_scheduler_config(req: SchedulerConfigRequest):
    config = get_config()
    auto_processor = get_auto_processor()

    schedule = load_schedule_config(config)
    schedule.update(
        {
            "enabled": req.enabled,
            "schedule_mode": req.scheduleMode,
            "start_time": req.startTime,
            "end_time": req.endTime,
            "language": req.language,
            "vlm_model": req.vlmModel,
            "parallel": req.parallel,
            "max_retries": req.maxRetries,
            "poll_interval_seconds": req.pollIntervalSeconds,
        }
    )
    save_schedule_config(config, schedule)

    if req.enabled:
        auto_processor.start()
    else:
        auto_processor.stop()

    return ok(
        {
            **_map_schedule(load_schedule_config(config)),
            "schedulerRunning": auto_processor.get_status().get("scheduler_running", False),
        }
    )


@router.get(
    "/status",
    summary="스케줄러 상태 조회",
    description="스케줄러 실행 상태, 미처리 파일 수, 현재 설정값을 함께 반환합니다. 운영 상태 모니터링에 적합합니다.",
)
def get_scheduler_status():
    config = get_config()
    auto_processor = get_auto_processor()
    return ok(
        {
            **auto_processor.get_status(),
            "unprocessedCount": len(scan_unprocessed_files(config)),
            "scheduleConfig": _map_schedule(load_schedule_config(config)),
        }
    )


@router.post(
    "/trigger",
    summary="스케줄러 수동 실행",
    description="현재 또는 요청 값 기준으로 즉시 자동 처리 배치를 한 번 실행합니다. 예약 시간 외 수동 실행 버튼에 연결할 수 있습니다.",
)
def trigger_scheduler(req: SchedulerTriggerRequest):
    auto_processor = get_auto_processor()
    count = auto_processor.trigger_now(
        req.language,
        req.vlmModel,
        parallel=req.parallel,
        max_retries=req.maxRetries,
    )

    if count == -1:
        fail("CONFLICT", "scheduler is already processing a batch", status=409)

    return ok(
        {
            "queuedCount": count,
            "started": count > 0,
        }
    )
