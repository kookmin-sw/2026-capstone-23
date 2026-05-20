import uuid
from pathlib import Path
from typing import Any, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from api.common import fail, fail_not_found, now_iso, ok
from api.dependencies import get_config
from api.security import (
    current_user_id,
    filter_records_for_user,
    get_request_user,
    request_auth_was_enforced,
    require_record_access,
)
from api.services.managed_files import resolve_managed_path
from core.rag import SimpleRAG
from infra.store import DOCUMENTS, RAG_SESSIONS


router = APIRouter(prefix="/rag", tags=["rag"])


class RagSessionCreateRequest(BaseModel):
    title: str = Field(default="문서 QA", min_length=1, max_length=120)
    documentIds: List[str] = Field(default_factory=list)
    documentPaths: List[str] = Field(default_factory=list)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("title must not be blank")
        return normalized


class RagMessageCreateRequest(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    topK: int = Field(default=3, ge=1, le=10)

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("content must not be blank")
        return normalized


class RagDocumentQueryRequest(BaseModel):
    path: str = Field(min_length=1)
    question: str = Field(min_length=1, max_length=4000)
    topK: int = Field(default=3, ge=1, le=10)

    @field_validator("path", "question")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized


def _require_session(session_id: str, request: Request | None = None):
    session = RAG_SESSIONS.get(session_id)
    if not session:
        fail_not_found("rag session", session_id)
    require_record_access(request, session, resource="rag session", resource_id=session_id)
    return session


def _require_documents_access(request: Request | None, document_ids: list[str]) -> None:
    for document_id in document_ids:
        doc = DOCUMENTS.get(document_id)
        if doc:
            require_record_access(request, doc, resource="document", resource_id=document_id)


def _resolve_output_path(raw_path: str) -> Path:
    config = get_config()
    try:
        path, _, _ = resolve_managed_path(config, raw_path, scopes=("output",))
    except FileNotFoundError:
        fail_not_found("output file", raw_path)
    except PermissionError:
        fail("FORBIDDEN_PATH", f"path is outside output root: {raw_path}", status=403)
    return path


def _try_resolve_output_path(raw_path: str) -> Path | None:
    try:
        return _resolve_output_path(raw_path)
    except HTTPException:
        return None


def _document_for_output_path(path: Path) -> tuple[str, dict] | None:
    target = path.resolve()
    for document_id, doc in DOCUMENTS.items():
        output_path = str(doc.get("outputPath") or "").strip()
        if not output_path:
            continue
        resolved = _try_resolve_output_path(output_path)
        if resolved is not None and resolved.resolve() == target:
            return str(document_id), doc
    return None


def _resolve_accessible_output_path(request: Request | None, raw_path: str) -> Path:
    path = _resolve_output_path(raw_path)
    matched = _document_for_output_path(path)
    if matched is not None:
        document_id, doc = matched
        require_record_access(request, doc, resource="document", resource_id=document_id)
        return path

    if request_auth_was_enforced(request) or get_request_user(request) is not None:
        fail(
            "FORBIDDEN_PATH",
            f"output file is not linked to an accessible document: {raw_path}",
            status=403,
        )
    return path


def _document_output_path(request: Request | None, document_id: str) -> Path | None:
    doc = DOCUMENTS.get(document_id)
    if not doc:
        return None
    require_record_access(request, doc, resource="document", resource_id=document_id)
    output_path = str(doc.get("outputPath") or "").strip()
    if not output_path:
        return None
    return _resolve_output_path(output_path)


def _session_output_paths(request: Request | None, session: dict) -> list[Path]:
    paths: list[Path] = []
    seen: set[str] = set()

    for document_id in session.get("documentIds") or []:
        path = _document_output_path(request, str(document_id))
        if path is None:
            continue
        key = str(path)
        if key not in seen:
            paths.append(path)
            seen.add(key)

    for raw_path in session.get("documentPaths") or []:
        path = _resolve_accessible_output_path(request, str(raw_path))
        key = str(path)
        if key not in seen:
            paths.append(path)
            seen.add(key)

    return paths


def _fallback_retrieval_payload(top_k: int, loaded_chunks: int) -> dict[str, Any]:
    return {
        "mode": "unavailable",
        "topK": top_k,
        "candidateChunks": loaded_chunks,
        "returnedChunks": 0,
        "embeddingModel": None,
        "timingMs": {"embedding": 0.0, "similarity": 0.0, "total": 0.0},
        "chunks": [],
    }


def _answer_from_paths(paths: list[Path], question: str, top_k: int) -> dict:
    if not paths:
        fail(
            "RAG_DOCUMENT_REQUIRED",
            "at least one completed output document is required for RAG",
            status=422,
        )

    try:
        rag = SimpleRAG()
        for path in paths:
            rag.load_document(path)
        if hasattr(rag, "answer_question_with_retrieval"):
            answer_payload = rag.answer_question_with_retrieval(question, top_k=top_k)
            answer = str(answer_payload.get("answer") or "")
            retrieval = answer_payload.get("retrieval") or _fallback_retrieval_payload(
                top_k,
                len(rag.documents),
            )
        else:
            answer = rag.answer_question(question, top_k=top_k)
            retrieval = _fallback_retrieval_payload(top_k, len(rag.documents))
    except ValueError as exc:
        fail("RAG_UNAVAILABLE", str(exc), status=503)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        fail("RAG_QUERY_FAILED", str(exc), status=500)

    citations = [{"path": str(path), "fileName": path.name} for path in paths]
    return {
        "answer": answer,
        "citations": citations,
        "loadedChunks": len(rag.documents),
        "retrieval": retrieval,
    }


@router.post(
    "/sessions",
    summary="RAG 세션 생성",
    description="문서 기반 질의응답 세션을 새로 만들고 세션 ID를 반환합니다. 채팅형 문서 QA 화면의 시작 지점입니다.",
)
def create_rag_session(request: Request, req: RagSessionCreateRequest):
    _require_documents_access(request, req.documentIds)
    for raw_path in req.documentPaths:
        _resolve_accessible_output_path(request, raw_path)
    session_id = f"s_{uuid.uuid4().hex[:10]}"
    RAG_SESSIONS[session_id] = {
        "sessionId": session_id,
        "ownerUserId": current_user_id(request),
        "title": req.title,
        "documentIds": req.documentIds,
        "documentPaths": req.documentPaths,
        "createdAt": now_iso(),
        "lastMessageAt": now_iso(),
        "messages": [],
    }
    return ok(
        {
            "sessionId": session_id,
            "title": req.title,
            "documentIds": req.documentIds,
            "documentPaths": req.documentPaths,
        }
    )


@router.get(
    "/sessions",
    summary="RAG 세션 목록 조회",
    description="기존 RAG 세션 목록을 반환합니다. 프론트에서 세션 사이드바나 최근 대화 목록을 구성할 때 사용합니다.",
)
def list_rag_sessions(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: Optional[str] = None,
):
    _ = cursor
    items = filter_records_for_user(request, RAG_SESSIONS.values())[:limit]
    mapped = [
        {
            "sessionId": session["sessionId"],
            "title": session["title"],
            "documentIds": session["documentIds"],
            "documentPaths": session.get("documentPaths", []),
            "lastMessageAt": session["lastMessageAt"],
            "createdAt": session["createdAt"],
        }
        for session in items
    ]
    return ok({"items": mapped, "nextCursor": None})


@router.get(
    "/sessions/{sessionId}",
    summary="RAG 세션 상세 조회",
    description="특정 세션의 제목, 연결 문서, 생성/수정 시각을 반환합니다. 세션 헤더 정보 표시에 사용합니다.",
)
def get_rag_session(request: Request, sessionId: str):
    session = _require_session(sessionId, request)
    return ok(
        {
            "sessionId": session["sessionId"],
            "title": session["title"],
            "documentIds": session["documentIds"],
            "documentPaths": session.get("documentPaths", []),
            "createdAt": session["createdAt"],
            "updatedAt": session["lastMessageAt"],
        }
    )


@router.get(
    "/sessions/{sessionId}/messages",
    summary="RAG 메시지 목록 조회",
    description="세션에 속한 대화 메시지 목록을 반환합니다. 채팅창 초기 렌더링이나 스크롤 페이징에 사용합니다.",
)
def get_rag_messages(
    request: Request,
    sessionId: str,
    limit: int = Query(default=30, ge=1, le=200),
    cursor: Optional[str] = None,
):
    _ = cursor
    session = _require_session(sessionId, request)
    return ok({"items": session["messages"][:limit], "nextCursor": None})


@router.post(
    "/sessions/{sessionId}/messages",
    summary="RAG 메시지 전송",
    description="사용자 질문을 세션에 추가하고 답변 초안을 반환합니다. 현재 구현은 세션 메시지 저장과 기본 응답 생성 중심입니다.",
)
def post_rag_message(request: Request, sessionId: str, req: RagMessageCreateRequest):
    session = _require_session(sessionId, request)

    user_msg = {"messageId": f"m_{uuid.uuid4().hex[:8]}", "role": "user", "content": req.content, "createdAt": now_iso()}
    rag_result = _answer_from_paths(
        _session_output_paths(request, session),
        req.content,
        req.topK,
    )
    bot_msg = {
        "messageId": f"m_{uuid.uuid4().hex[:8]}",
        "role": "assistant",
        "content": f"(초안 응답) {req.content}",
        "citations": [],
        "createdAt": now_iso(),
    }
    bot_msg["content"] = rag_result["answer"]
    bot_msg["citations"] = rag_result["citations"]
    bot_msg["retrieval"] = rag_result["retrieval"]
    session["messages"].extend([user_msg, bot_msg])
    session["lastMessageAt"] = now_iso()
    return ok(
        {
            "sessionId": sessionId,
            "answer": bot_msg["content"],
            "citations": bot_msg["citations"],
            "loadedChunks": rag_result["loadedChunks"],
            "retrieval": rag_result["retrieval"],
        }
    )


@router.delete(
    "/sessions/{sessionId}",
    summary="RAG 세션 삭제",
    description="특정 RAG 세션과 그 안의 메시지를 삭제합니다. 대화 목록 정리 기능에 연결할 수 있습니다.",
)
def delete_rag_session(request: Request, sessionId: str):
    if sessionId not in RAG_SESSIONS:
        fail_not_found("rag session", sessionId)
    require_record_access(request, RAG_SESSIONS[sessionId], resource="rag session", resource_id=sessionId)
    del RAG_SESSIONS[sessionId]
    return ok({"ok": True, "sessionId": sessionId, "deletedAt": now_iso()})


@router.post(
    "/query",
    summary="문서 단발성 RAG 질의",
    description="세션 없이 특정 출력 문서를 대상으로 질문 1건을 실행합니다. 결과 확인용 단발성 QA나 관리자 도구에 적합합니다.",
)
def query_document(request: Request, req: RagDocumentQueryRequest):
    path = _resolve_accessible_output_path(request, req.path)
    rag_result = _answer_from_paths([path], req.question, req.topK)

    return ok(
        {
            "path": str(path),
            "question": req.question,
            "topK": req.topK,
            "loadedChunks": rag_result["loadedChunks"],
            "answer": rag_result["answer"],
            "citations": rag_result["citations"],
            "retrieval": rag_result["retrieval"],
        }
    )
