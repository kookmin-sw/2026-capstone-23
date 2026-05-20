from typing import Optional

from fastapi import APIRouter, Query, WebSocket
from starlette.websockets import WebSocketDisconnect

from api.common import now_iso, ok
from api.security import authenticate_websocket
from api.ws_hub import JOB_WS_HUB
from infra.store import JOBS


router = APIRouter(tags=["websocket"])


@router.websocket("/ws/jobs")
async def ws_jobs(websocket: WebSocket, jobId: Optional[str] = Query(default=None)):
    await websocket.accept()
    await JOB_WS_HUB.register(websocket, job_id=jobId)
    await websocket.send_json(
        ok(
            {
                "type": "job.item.progress",
                "jobId": jobId,
                "documentId": None,
                "status": "CONNECTED",
                "percent": 0,
                "message": "job websocket connected",
                "timestamp": now_iso(),
            }
        )
    )
    if jobId and jobId in JOBS:
        job = JOBS[jobId]
        total = int(job.get("totalDocuments", job.get("totalItems", 0)) or 0)
        finished = (
            int(job.get("completedDocuments", job.get("completedItems", 0)) or 0)
            + int(job.get("failedDocuments", job.get("failedItems", 0)) or 0)
            + int(job.get("canceledDocuments", job.get("canceledItems", 0)) or 0)
        )
        stored_percent = job.get("progressPercent")
        if stored_percent is not None:
            percent = min(100, max(0, int(stored_percent)))
        else:
            percent = min(100, max(0, int((finished / total) * 100))) if total > 0 else 0
        await websocket.send_json(
            ok(
                {
                    "type": "job.item.progress",
                    "jobId": jobId,
                    "documentId": None,
                    "status": job.get("status"),
                    "eventType": "SNAPSHOT",
                    "percent": percent,
                    "jobPercent": percent,
                    "progressPercent": percent,
                    "documentPercent": None,
                    "totalDocuments": job.get("totalDocuments"),
                    "completedDocuments": job.get("completedDocuments"),
                    "failedDocuments": job.get("failedDocuments"),
                    "canceledDocuments": job.get("canceledDocuments"),
                    "timestamp": now_iso(),
                }
            )
        )
    try:
        while True:
            # 클라이언트 ping/메시지를 수신해 연결을 유지한다.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await JOB_WS_HUB.unregister(websocket)


@router.websocket("/ws/system")
async def ws_system(websocket: WebSocket):
    user = await authenticate_websocket(websocket, require_admin=True)
    if user is None:
        return
    await websocket.accept()
    await websocket.send_json(
        ok(
            {
                "type": "system.metrics",
                "cpu": 0.0,
                "memory": {"usedBytes": 0, "totalBytes": 0},
                "activeWorkers": 0,
                "queueDepth": 0,
                "timestamp": now_iso(),
            }
        )
    )
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
