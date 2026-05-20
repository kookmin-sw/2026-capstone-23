from typing import Optional

from fastapi import APIRouter, Query, Request

from api.common import ok
from api.security import filter_records_for_user
from core.documents.records import document_record_for_display
from infra.store import DOCUMENTS


router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get(
    "/summary",
    summary="대시보드 요약 통계",
    description="총 문서 수, 완료 수, 실패 수, 처리 중 수를 집계해 반환합니다. 메인 대시보드 카드형 요약 영역에 사용합니다.",
)
def dashboard_summary(
    request: Request,
    from_: Optional[str] = Query(default=None, alias="from"),
    to: Optional[str] = Query(default=None),
):
    _ = (from_, to)
    documents = filter_records_for_user(request, DOCUMENTS.values())
    total = len(documents)
    completed = len(
        [doc for doc in documents if doc.get("latestStatus") == "COMPLETED"]
    )
    failed = len([doc for doc in documents if doc.get("latestStatus") == "FAILED"])
    processing = len(
        [
            doc
            for doc in documents
            if doc.get("latestStatus") not in {"COMPLETED", "FAILED", "CANCELED"}
        ]
    )
    return ok(
        {
            "totalJobs": total,
            "completedJobs": completed,
            "processingJobs": processing,
            "failedJobs": failed,
        }
    )


@router.get(
    "/file-types",
    summary="문서 파일 형식 통계",
    description="현재 저장된 문서들의 파일 형식 종류를 반환합니다. 필터 UI나 간단한 분포 차트에 사용할 수 있습니다.",
)
def dashboard_file_types(request: Request):
    counts: dict[str, int] = {}
    for doc in filter_records_for_user(request, DOCUMENTS.values()):
        file_type = str(doc.get("fileType") or "unknown")
        counts[file_type] = counts.get(file_type, 0) + 1

    items = [
        {"type": file_type, "count": count}
        for file_type, count in sorted(counts.items())
    ]
    return ok(
        {
            "from": None,
            "to": None,
            "types": [item["type"] for item in items],
            "items": items,
        }
    )


@router.get(
    "/recent-items",
    summary="최근 문서 항목 조회",
    description="최근 문서 항목 목록을 제한 개수만큼 반환합니다. 최근 처리 내역 테이블이나 활동 피드에 적합합니다.",
)
def dashboard_recent_items(
    request: Request,
    limit: int = Query(default=10, ge=1, le=100),
    cursor: Optional[str] = None,
):
    _ = cursor
    visible_documents = filter_records_for_user(request, DOCUMENTS.values())
    items = sorted(
        visible_documents,
        key=lambda item: (
            str(item.get("updatedAt") or item.get("uploadedAt") or ""),
            str(item.get("documentId") or ""),
        ),
        reverse=True,
    )[:limit]
    return ok(
        {
            "items": [document_record_for_display(item) for item in items],
            "nextCursor": None,
        }
    )
