import os
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("STORE_BACKEND", "sqlite")

import api.routers.rag as rag_module
from infra.store import DOCUMENTS, RAG_SESSIONS


class FakeRAG:
    def __init__(self, model: str = "gpt-4o-mini"):
        self.model = model
        self.documents = []

    def load_document(self, path: Path):
        self.documents.append({"source": str(path), "chunk_id": len(self.documents)})

    def answer_question(self, question: str, top_k: int = 3) -> str:
        return f"answer:{question}:{top_k}:{len(self.documents)}"

    def answer_question_with_retrieval(self, question: str, top_k: int = 3):
        return {
            "answer": self.answer_question(question, top_k=top_k),
            "retrieval": {
                "mode": "semantic",
                "topK": top_k,
                "candidateChunks": len(self.documents),
                "returnedChunks": min(top_k, len(self.documents)),
                "embeddingModel": "fake-embedding",
                "timingMs": {"embedding": 1.0, "similarity": 0.1, "total": 1.1},
                "chunks": [
                    {
                        "rank": index + 1,
                        "score": 1.0,
                        "source": str(document["source"]),
                        "fileName": Path(str(document["source"])).name,
                        "chunkId": document["chunk_id"],
                        "textPreview": "",
                    }
                    for index, document in enumerate(self.documents[:top_k])
                ],
            },
        }


def make_client(tmp_path: Path, monkeypatch, user_id: str | None = None) -> TestClient:
    config = SimpleNamespace(
        input_root=tmp_path / "inputs",
        output_root=tmp_path / "outputs",
        tmp_root=tmp_path / "tmp",
    )
    monkeypatch.setattr(rag_module, "SimpleRAG", FakeRAG)
    monkeypatch.setattr(rag_module, "get_config", lambda: config)

    app = FastAPI()

    if user_id is not None:

        @app.middleware("http")
        async def inject_user(request, call_next):
            request.state.auth_checked = True
            request.state.current_user = {"userId": user_id, "role": "USER"}
            return await call_next(request)

    app.include_router(rag_module.router, prefix="/api/v1")
    return TestClient(app)


def setup_function():
    RAG_SESSIONS.clear()
    DOCUMENTS.clear()


def _write_output(tmp_path: Path, name: str = "sample.txt") -> Path:
    output_file = tmp_path / "outputs" / name
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text("sample document", encoding="utf-8")
    return output_file


def test_rag_session_lifecycle_with_output_path(tmp_path: Path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    output_file = _write_output(tmp_path)

    create_res = client.post(
        "/api/v1/rag/sessions",
        json={"title": " QA Session ", "documentPaths": [str(output_file)]},
    )
    assert create_res.status_code == 200
    created = create_res.json()["data"]
    session_id = created["sessionId"]
    assert created["title"] == "QA Session"
    assert created["documentPaths"] == [str(output_file)]

    message_res = client.post(
        f"/api/v1/rag/sessions/{session_id}/messages",
        json={"content": " summarize ", "topK": 2},
    )
    assert message_res.status_code == 200
    data = message_res.json()["data"]
    assert data["answer"] == "answer:summarize:2:1"
    assert data["loadedChunks"] == 1
    assert data["citations"] == [{"path": str(output_file), "fileName": "sample.txt"}]
    assert data["retrieval"]["mode"] == "semantic"
    assert data["retrieval"]["chunks"][0]["chunkId"] == 0

    list_res = client.get(f"/api/v1/rag/sessions/{session_id}/messages")
    assert list_res.status_code == 200
    assert len(list_res.json()["data"]["items"]) == 2

    sessions_res = client.get("/api/v1/rag/sessions")
    assert sessions_res.status_code == 200
    sessions = sessions_res.json()["data"]["items"]
    assert len(sessions) == 1
    assert sessions[0]["sessionId"] == session_id
    assert sessions[0]["documentPaths"] == [str(output_file)]
    assert sessions[0]["lastMessageAt"] is not None

    delete_res = client.delete(f"/api/v1/rag/sessions/{session_id}")
    assert delete_res.status_code == 200


def test_rag_session_message_uses_document_id_output_path(tmp_path: Path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)
    output_file = _write_output(tmp_path, "from-document-id.txt")
    DOCUMENTS["d_1"] = {
        "documentId": "d_1",
        "outputPath": str(output_file),
        "latestStatus": "COMPLETED",
    }

    created = client.post(
        "/api/v1/rag/sessions",
        json={"title": "document session", "documentIds": ["d_1"]},
    ).json()["data"]

    message_res = client.post(
        f"/api/v1/rag/sessions/{created['sessionId']}/messages",
        json={"content": "what is this?"},
    )
    assert message_res.status_code == 200
    assert message_res.json()["data"]["answer"] == "answer:what is this?:3:1"


def test_rag_session_rejects_cross_owner_document_path(tmp_path: Path, monkeypatch):
    client = make_client(tmp_path, monkeypatch, user_id="owner")
    output_file = _write_output(tmp_path, "other-owner.txt")
    DOCUMENTS["d_other"] = {
        "documentId": "d_other",
        "ownerUserId": "other",
        "outputPath": str(output_file),
        "latestStatus": "COMPLETED",
    }

    response = client.post(
        "/api/v1/rag/sessions",
        json={"title": "blocked", "documentPaths": [str(output_file)]},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["error"]["code"] == "FORBIDDEN"


def test_rag_validation_and_not_found(tmp_path: Path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)

    not_found = client.get("/api/v1/rag/sessions/s_missing")
    assert not_found.status_code == 404
    assert not_found.json()["detail"]["error"]["code"] == "NOT_FOUND"

    invalid_title = client.post("/api/v1/rag/sessions", json={"title": "   ", "documentIds": []})
    assert invalid_title.status_code == 422

    created = client.post("/api/v1/rag/sessions", json={"title": "ok", "documentIds": []}).json()["data"]
    invalid_message = client.post(
        f"/api/v1/rag/sessions/{created['sessionId']}/messages",
        json={"content": "   "},
    )
    assert invalid_message.status_code == 422


def test_rag_message_requires_linked_output_document(tmp_path: Path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)

    created = client.post("/api/v1/rag/sessions", json={"title": "empty", "documentIds": []}).json()["data"]
    response = client.post(
        f"/api/v1/rag/sessions/{created['sessionId']}/messages",
        json={"content": "summarize"},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["error"]["code"] == "RAG_DOCUMENT_REQUIRED"
