import importlib
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("OPENAI_API_KEY", "test-key")


class FakeRAG:
    def __init__(self, model="gpt-4o-mini"):
        self.model = model
        self.documents = []

    def load_document(self, path: Path):
        self.documents = [{"source": str(path)}] * 3

    def answer_question(self, question: str, top_k: int = 3) -> str:
        return f"answer:{question}:{top_k}"

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
                        "chunkId": index,
                        "textPreview": "",
                    }
                    for index, document in enumerate(self.documents[:top_k])
                ],
            },
        }


def make_client(tmp_path: Path, monkeypatch) -> TestClient:
    deps_module = types.ModuleType("api.dependencies")
    deps_module.config = SimpleNamespace(
        input_root=tmp_path / "inputs",
        output_root=tmp_path / "outputs",
        tmp_root=tmp_path / "tmp",
    )
    deps_module.pipeline = SimpleNamespace(
        update_vlm_model=lambda *_args, **_kwargs: None,
        process_file=lambda *_args, **_kwargs: None,
    )
    deps_module.get_auto_processor = lambda: SimpleNamespace(
        start=lambda: True,
        stop=lambda: None,
        trigger_now=lambda *_args, **_kwargs: 0,
        get_status=lambda: {"scheduler_running": False, "processing": False},
    )
    deps_module.get_config = lambda: deps_module.config
    deps_module.set_runtime_services = lambda **_kwargs: None

    original_dependencies = sys.modules.get("api.dependencies")
    try:
        sys.modules["api.dependencies"] = deps_module
        import api.routers.rag as rag_module

        rag_module = importlib.reload(rag_module)
    finally:
        if original_dependencies is None:
            sys.modules.pop("api.dependencies", None)
        else:
            sys.modules["api.dependencies"] = original_dependencies

    monkeypatch.setattr(rag_module, "SimpleRAG", FakeRAG)

    app = FastAPI()
    app.include_router(rag_module.router, prefix="/api/v1")
    return TestClient(app)


def test_rag_query_uses_output_file(tmp_path: Path, monkeypatch):
    client = make_client(tmp_path, monkeypatch)

    output_file = tmp_path / "outputs" / "sample.txt"
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text("테스트 문서", encoding="utf-8")

    res = client.post(
        "/api/v1/rag/query",
        json={"path": str(output_file), "question": "요약", "topK": 2},
    )
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["answer"] == "answer:요약:2"
    assert data["loadedChunks"] == 3
    assert data["retrieval"]["topK"] == 2
    assert data["retrieval"]["returnedChunks"] == 2
