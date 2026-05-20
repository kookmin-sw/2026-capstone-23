from fastapi import APIRouter, Response

from api.common import ok
from core.version import API_VERSION, APP_VERSION
from infra.queue.job_queue import queue_status


router = APIRouter(tags=["health"])


@router.get(
    "/health",
    summary="헬스 체크",
    description="백엔드 프로세스가 정상 응답 중인지 확인합니다. 로드밸런서나 운영 점검에서 가장 먼저 호출하는 엔드포인트입니다.",
)
def health():
    return ok({"status": "ok", "version": APP_VERSION, "apiVersion": API_VERSION})


@router.get(
    "/health/ready",
    summary="Readiness check",
    description="Returns runtime readiness, including whether the configured job queue can accept work.",
)
def readiness(response: Response):
    queue = queue_status()
    is_ready = bool(queue["available"])
    if not is_ready:
        response.status_code = 503
    return ok({
        "status": "ok" if queue["available"] else "degraded",
        "version": APP_VERSION,
        "apiVersion": API_VERSION,
        "queue": queue,
    })
