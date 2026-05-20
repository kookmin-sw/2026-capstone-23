import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np

import core.rag as rag_module
from core.rag import SimpleRAG


class FakeEmbeddings:
    def __init__(self):
        self.calls = []

    def create(self, *, model: str, input: list[str]):
        self.calls.append((model, list(input)))
        data = [
            SimpleNamespace(embedding=[float(index + 1), 0.0])
            for index, _ in enumerate(input)
        ]
        return SimpleNamespace(data=data)


class FakeChatCompletions:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        prompt = kwargs["messages"][-1]["content"]
        if "[부분 추출 결과]" in prompt:
            content = "김철수\n박영희\n이민수"
        elif "부분 결과:" in prompt:
            if "김철수" in prompt:
                content = "김철수"
            elif "박영희" in prompt:
                content = "박영희\n이민수"
            else:
                content = "없음"
        else:
            content = "answer"
        message = SimpleNamespace(content=content)
        choice = SimpleNamespace(message=message)
        return SimpleNamespace(choices=[choice])


class FakeClient:
    def __init__(self):
        self.embeddings = FakeEmbeddings()
        self.chat = SimpleNamespace(completions=FakeChatCompletions())


def make_rag() -> SimpleRAG:
    previous_provider = os.environ.get("RAG_EMBEDDING_PROVIDER")
    os.environ["RAG_EMBEDDING_PROVIDER"] = "openai"
    try:
        rag = SimpleRAG(api_key="test-key", provider="openai")
    finally:
        if previous_provider is None:
            os.environ.pop("RAG_EMBEDDING_PROVIDER", None)
        else:
            os.environ["RAG_EMBEDDING_PROVIDER"] = previous_provider
    fake_client = FakeClient()
    rag.client = fake_client
    rag.embedding_client = fake_client
    return rag


def test_openrouter_client_configuration(monkeypatch):
    captured_kwargs = []

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured_kwargs.append(kwargs)

    monkeypatch.setattr(rag_module, "OpenAI", FakeOpenAI)
    monkeypatch.setenv("RAG_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-embedding-key")
    monkeypatch.setenv("RAG_CHAT_MODEL", "openrouter/qwen3-vl-32b")
    monkeypatch.setenv("RAG_EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("RAG_EMBEDDING_MODEL", "text-embedding-3-small")
    monkeypatch.setenv("RAG_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("OPENROUTER_HTTP_REFERER", "http://localhost:3000")
    monkeypatch.setenv("OPENROUTER_APP_TITLE", "Luminir Test")

    rag = SimpleRAG()

    assert rag.provider == "openrouter"
    assert rag.model == "qwen/qwen3-vl-32b-instruct"
    assert rag.embedding_model == "text-embedding-3-small"
    assert rag.base_url == "https://openrouter.ai/api/v1"
    assert captured_kwargs == [
        {
            "api_key": "or-test-key",
            "base_url": "https://openrouter.ai/api/v1",
            "default_headers": {
                "HTTP-Referer": "http://localhost:3000",
                "X-OpenRouter-Title": "Luminir Test",
            },
        },
        {"api_key": "openai-embedding-key"},
    ]


def test_openrouter_embedding_client_configuration(monkeypatch):
    captured_kwargs = []

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured_kwargs.append(kwargs)

    monkeypatch.setattr(rag_module, "OpenAI", FakeOpenAI)
    monkeypatch.setenv("RAG_PROVIDER", "openrouter")
    monkeypatch.setenv("RAG_EMBEDDING_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")
    monkeypatch.setenv("RAG_CHAT_MODEL", "openrouter/openai/gpt-5.2")
    monkeypatch.setenv("RAG_EMBEDDING_MODEL", "openrouter/openai/text-embedding-3-small")
    monkeypatch.setenv("RAG_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("OPENROUTER_HTTP_REFERER", "http://localhost:3000")
    monkeypatch.setenv("OPENROUTER_APP_TITLE", "Luminir Test")

    rag = SimpleRAG()

    assert rag.provider == "openrouter"
    assert rag.model == "openai/gpt-5.2"
    assert rag.embedding_provider == "openrouter"
    assert rag.embedding_model == "openai/text-embedding-3-small"
    assert captured_kwargs == [
        {
            "api_key": "or-test-key",
            "base_url": "https://openrouter.ai/api/v1",
            "default_headers": {
                "HTTP-Referer": "http://localhost:3000",
                "X-OpenRouter-Title": "Luminir Test",
            },
        },
        {
            "api_key": "or-test-key",
            "base_url": "https://openrouter.ai/api/v1",
            "default_headers": {
                "HTTP-Referer": "http://localhost:3000",
                "X-OpenRouter-Title": "Luminir Test",
            },
        },
    ]


def test_openai_provider_remains_supported(monkeypatch):
    captured_kwargs = []

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured_kwargs.append(kwargs)

    monkeypatch.setattr(rag_module, "OpenAI", FakeOpenAI)
    monkeypatch.setenv("RAG_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-test-key")
    monkeypatch.setenv("RAG_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("RAG_EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("RAG_EMBEDDING_MODEL", "text-embedding-3-small")
    monkeypatch.setenv("RAG_BASE_URL", "")
    monkeypatch.setenv("OPENAI_BASE_URL", "")

    rag = SimpleRAG()

    assert rag.provider == "openai"
    assert rag.model == "gpt-4o-mini"
    assert rag.embedding_model == "text-embedding-3-small"
    assert rag.base_url == ""
    assert captured_kwargs == [
        {"api_key": "openai-test-key"},
        {"api_key": "openai-test-key"},
    ]


def test_openai_embedding_falls_back_to_openrouter(monkeypatch):
    captured_kwargs = []

    class FailingEmbeddings:
        def create(self, *, model: str, input: list[str]):
            raise RuntimeError("insufficient_quota")

    class WorkingEmbeddings:
        def __init__(self):
            self.calls = []

        def create(self, *, model: str, input: list[str]):
            self.calls.append((model, list(input)))
            return SimpleNamespace(
                data=[SimpleNamespace(embedding=[1.0, 0.0]) for _ in input]
            )

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured_kwargs.append(kwargs)
            self.chat = SimpleNamespace(completions=FakeChatCompletions())
            if kwargs.get("base_url") == "https://openrouter.ai/api/v1":
                self.embeddings = WorkingEmbeddings()
            else:
                self.embeddings = FailingEmbeddings()

    monkeypatch.setattr(rag_module, "OpenAI", FakeOpenAI)
    monkeypatch.setenv("RAG_PROVIDER", "openai")
    monkeypatch.setenv("RAG_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("RAG_EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("RAG_EMBEDDING_MODEL", "text-embedding-3-small")
    monkeypatch.setenv("RAG_OPENROUTER_EMBEDDING_MODEL", "openai/text-embedding-3-small")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-test-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")
    monkeypatch.setenv("RAG_BASE_URL", "")
    monkeypatch.setenv("OPENAI_BASE_URL", "")

    rag = SimpleRAG()
    embeddings = rag.get_embeddings(["fallback"])

    assert embeddings == [[1.0, 0.0]]
    assert rag.embedding_provider == "openrouter"
    assert rag.embedding_model == "openai/text-embedding-3-small"
    assert captured_kwargs[-1] == {
        "api_key": "or-test-key",
        "base_url": "https://openrouter.ai/api/v1",
    }


def test_load_document_batches_embeddings(tmp_path: Path):
    SimpleRAG._document_cache.clear()
    document = tmp_path / "sample.txt"
    document.write_text(
        "\n\n".join(f"{index}-" + ("x" * 501) for index in range(120)),
        encoding="utf-8",
    )

    rag = make_rag()
    rag.load_document(document)

    assert len(rag.documents) == 120
    assert len(rag.client.embeddings.calls) == 2


def test_load_document_uses_memory_cache(tmp_path: Path):
    SimpleRAG._document_cache.clear()
    document = tmp_path / "sample.txt"
    document.write_text("cached document", encoding="utf-8")

    first = make_rag()
    first.load_document(document)

    second = make_rag()
    second.load_document(document)

    assert len(second.documents) == len(first.documents)
    assert second.client.embeddings.calls == []
    assert second.source_texts[str(document)] == "cached document"


def test_retrieve_returns_rank_score_and_timing():
    rag = make_rag()
    rag.documents = [
        {
            "text": "alpha",
            "embedding": np.array([1.0, 0.0]),
            "source": "/tmp/a.txt",
            "chunk_id": 0,
        },
        {
            "text": "beta",
            "embedding": np.array([0.0, 1.0]),
            "source": "/tmp/b.txt",
            "chunk_id": 1,
        },
    ]
    rag.get_embedding = lambda _query: [0.0, 1.0]

    report = rag.retrieve("beta question", top_k=1)

    assert report["mode"] == "semantic"
    assert report["candidateChunks"] == 2
    assert report["returnedChunks"] == 1
    assert report["chunks"][0]["rank"] == 1
    assert report["chunks"][0]["score"] == 1.0
    assert report["chunks"][0]["source"] == "/tmp/b.txt"
    assert report["timingMs"]["total"] >= 0.0


def test_answer_question_with_retrieval_returns_public_chunks():
    rag = make_rag()
    rag.documents = [
        {
            "text": "alpha answer",
            "embedding": np.array([1.0, 0.0]),
            "source": "/tmp/a.txt",
            "chunk_id": 7,
        },
    ]
    rag.get_embedding = lambda _query: [1.0, 0.0]

    result = rag.answer_question_with_retrieval("alpha?", top_k=1)

    assert result["answer"] == "answer"
    retrieval = result["retrieval"]
    assert retrieval["mode"] == "semantic"
    assert retrieval["topK"] == 1
    assert retrieval["chunks"] == [
        {
            "rank": 1,
            "score": 1.0,
            "source": "/tmp/a.txt",
            "fileName": "a.txt",
            "chunkId": 7,
            "textPreview": "alpha answer",
        }
    ]


def test_exhaustive_question_scans_source_text_without_query_embedding():
    rag = make_rag()
    rag.source_texts = {
        "sample.txt": "김철수\n\n" + ("중간 내용 " * 700) + "\n\n박영희 이민수",
    }
    rag.exhaustive_chunk_size = 1000
    rag.exhaustive_chunk_overlap = 0

    result = rag.answer_question_with_retrieval("문서 내 이름 전부 알려줘")
    answer = result["answer"]

    assert "김철수" in answer
    assert "박영희" in answer
    assert "이민수" in answer
    assert result["retrieval"]["mode"] == "exhaustive"
    assert rag.client.embeddings.calls == []
    assert len(rag.client.chat.completions.calls) >= 3
