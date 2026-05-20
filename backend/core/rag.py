from __future__ import annotations

import threading
from pathlib import Path
from time import perf_counter
from typing import Any

import numpy as np
from openai import OpenAI

from core.env import env_int, env_str


OPENAI_PROVIDER = "openai"
OPENROUTER_PROVIDER = "openrouter"
DEFAULT_OPENAI_MODEL = "gpt-5.2"
DEFAULT_OPENAI_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_OPENROUTER_MODEL = "openrouter/openai/gpt-5.2"
DEFAULT_OPENROUTER_EMBEDDING_MODEL = "openai/text-embedding-3-small"
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_MISSING_SECRET_VALUES = {
    "",
    "replace_with_openai_api_key",
    "replace_with_openrouter_api_key",
    "your_openai_api_key",
    "your_openrouter_api_key",
}
_OPENROUTER_MODEL_ALIASES = {
    "gpt-5.2": "openai/gpt-5.2",
    "gpt-5-mini": "openai/gpt-5-mini",
    "openrouter/gpt-5.2": "openai/gpt-5.2",
    "openrouter/gpt-5-mini": "openai/gpt-5-mini",
    "openrouter/qwen3-vl-8b": "qwen/qwen3-vl-8b-instruct",
    "openrouter/qwen3-vl-32b": "qwen/qwen3-vl-32b-instruct",
}
_OPENROUTER_EMBEDDING_MODEL_ALIASES = {
    "text-embedding-3-small": "openai/text-embedding-3-small",
    "text-embedding-3-large": "openai/text-embedding-3-large",
    "openrouter/text-embedding-3-small": "openai/text-embedding-3-small",
    "openrouter/text-embedding-3-large": "openai/text-embedding-3-large",
    "openrouter/openai/text-embedding-3-small": "openai/text-embedding-3-small",
    "openrouter/openai/text-embedding-3-large": "openai/text-embedding-3-large",
}


def _secret_or_none(value: str | None) -> str | None:
    candidate = (value or "").strip()
    if candidate.lower() in _MISSING_SECRET_VALUES:
        return None
    return candidate


def _env_first(*names: str) -> str:
    for name in names:
        value = env_str(name, "", strip=True)
        if value:
            return value
    return ""


def _normalize_provider(provider: str | None) -> str | None:
    normalized = (provider or "").strip().lower()
    if not normalized:
        return None
    if normalized not in {OPENAI_PROVIDER, OPENROUTER_PROVIDER}:
        raise ValueError(f"unsupported RAG provider: {provider}")
    return normalized


def _normalize_embedding_provider(provider: str | None) -> str | None:
    normalized = (provider or "").strip().lower()
    if not normalized:
        return None
    if normalized not in {OPENAI_PROVIDER, OPENROUTER_PROVIDER}:
        raise ValueError(f"unsupported RAG embedding provider: {provider}")
    return normalized


def _normalize_openrouter_model(model: str) -> str:
    normalized = model.strip()
    lowered = normalized.lower()
    if lowered in _OPENROUTER_MODEL_ALIASES:
        return _OPENROUTER_MODEL_ALIASES[lowered]
    if lowered.startswith("openrouter/"):
        return normalized[len("openrouter/") :]
    return normalized


def _normalize_openrouter_embedding_model(model: str) -> str:
    normalized = model.strip()
    lowered = normalized.lower()
    if lowered in _OPENROUTER_EMBEDDING_MODEL_ALIASES:
        return _OPENROUTER_EMBEDDING_MODEL_ALIASES[lowered]
    if lowered.startswith("openrouter/"):
        return normalized[len("openrouter/") :]
    if "/" not in normalized:
        return f"openai/{normalized}"
    return normalized


def _normalize_openai_model(model: str) -> str:
    normalized = model.strip()
    lowered = normalized.lower()
    if lowered.startswith("openrouter/openai/"):
        return normalized[len("openrouter/openai/") :]
    if lowered.startswith("openai/"):
        return normalized[len("openai/") :]
    return normalized


def _normalize_openai_embedding_model(model: str) -> str:
    normalized = model.strip()
    lowered = normalized.lower()
    if lowered.startswith("openrouter/openai/"):
        return normalized[len("openrouter/openai/") :]
    if lowered.startswith("openai/"):
        return normalized[len("openai/") :]
    return normalized


def _openrouter_headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    referer = _env_first("RAG_OPENROUTER_HTTP_REFERER", "OPENROUTER_HTTP_REFERER")
    title = _env_first("RAG_OPENROUTER_APP_TITLE", "OPENROUTER_APP_TITLE")
    if referer:
        headers["HTTP-Referer"] = referer
    if title:
        headers["X-OpenRouter-Title"] = title
    return headers


class SimpleRAG:
    """Small OpenAI-compatible RAG helper for converted text outputs."""

    _cache_lock = threading.RLock()
    _document_cache: dict[tuple[str, int, int, str, str], list[dict[str, Any]]] = {}
    _max_cached_documents = 16
    _exhaustive_markers = (
        "전부",
        "전체",
        "모든",
        "모두",
        "다 알려",
        "다 찾아",
        "빠짐없이",
        "목록",
        "나열",
        "추출",
        "all",
        "every",
        "entire",
        "whole",
        "list",
        "extract",
        "enumerate",
    )

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        *,
        provider: str | None = None,
        embedding_model: str | None = None,
        base_url: str | None = None,
    ):
        self.provider = self._resolve_provider(provider, model)
        self.model = self._resolve_model(model)
        self.base_url = self._resolve_base_url(base_url)
        self.api_key = _secret_or_none(api_key) or self._resolve_chat_api_key()
        if not self.api_key:
            if self.provider == OPENROUTER_PROVIDER:
                raise ValueError("OPENROUTER_API_KEY is required for OpenRouter RAG answers")
            raise ValueError("OPENAI_API_KEY is required for RAG answers")

        client_kwargs: dict[str, Any] = {"api_key": self.api_key}
        if self.base_url:
            client_kwargs["base_url"] = self.base_url
        if self.provider == OPENROUTER_PROVIDER:
            headers = _openrouter_headers()
            if headers:
                client_kwargs["default_headers"] = headers
        self.client = OpenAI(**client_kwargs)

        self.embedding_provider = self._resolve_embedding_provider()
        self.embedding_model = self._resolve_embedding_model(embedding_model)
        self.embedding_base_url = self._resolve_embedding_base_url()
        self.embedding_api_key = self._resolve_embedding_api_key(api_key)
        if not self.embedding_api_key:
            if self.embedding_provider == OPENROUTER_PROVIDER:
                raise ValueError("OPENROUTER_API_KEY is required for OpenRouter RAG embeddings")
            raise ValueError("OPENAI_API_KEY is required for RAG embeddings")
        self.embedding_client = self._build_embedding_client()
        self.embedding_cache_namespace = self.embedding_base_url or self.embedding_provider
        self.documents: list[dict[str, Any]] = []
        self.source_texts: dict[str, str] = {}
        self.exhaustive_chunk_size = env_int(
            "RAG_EXHAUSTIVE_CHUNK_SIZE",
            3500,
            minimum=1000,
            maximum=12000,
        )
        self.exhaustive_chunk_overlap = env_int(
            "RAG_EXHAUSTIVE_CHUNK_OVERLAP",
            150,
            minimum=0,
            maximum=1000,
        )
        # 0 means no limit. Exhaustive questions should scan the whole document by default.
        self.exhaustive_max_chunks = env_int(
            "RAG_EXHAUSTIVE_MAX_CHUNKS",
            0,
            minimum=0,
            maximum=500,
        )

    @staticmethod
    def _resolve_provider(provider: str | None, model: str | None) -> str:
        normalized_provider = _normalize_provider(provider or env_str("RAG_PROVIDER", ""))
        if normalized_provider:
            return normalized_provider

        configured_model = model or _env_first("RAG_CHAT_MODEL", "RAG_MODEL")
        if configured_model.lower().startswith("openrouter/"):
            return OPENROUTER_PROVIDER
        if configured_model:
            return OPENAI_PROVIDER
        return OPENROUTER_PROVIDER

    def _resolve_chat_api_key(self) -> str | None:
        rag_api_key = _secret_or_none(env_str("RAG_API_KEY"))
        if rag_api_key:
            return rag_api_key
        if self.provider == OPENROUTER_PROVIDER:
            return _secret_or_none(env_str("OPENROUTER_API_KEY"))
        return _secret_or_none(env_str("OPENAI_API_KEY"))

    def _resolve_model(self, model: str | None) -> str:
        if self.provider == OPENROUTER_PROVIDER:
            raw_model = (
                model
                or _env_first("RAG_CHAT_MODEL")
                or _env_first("RAG_MODEL", "RAG_OPENROUTER_MODEL")
                or DEFAULT_OPENROUTER_MODEL
            )
            return _normalize_openrouter_model(raw_model)

        raw_model = (
            model
            or _env_first("RAG_OPENAI_MODEL", "RAG_MODEL", "RAG_CHAT_MODEL")
            or DEFAULT_OPENAI_MODEL
        )
        return _normalize_openai_model(raw_model)

    def _resolve_embedding_model(self, embedding_model: str | None) -> str:
        if self.embedding_provider == OPENROUTER_PROVIDER:
            raw_model = (
                embedding_model
                or _env_first("RAG_OPENROUTER_EMBEDDING_MODEL")
                or _env_first("RAG_EMBEDDING_MODEL")
                or DEFAULT_OPENROUTER_EMBEDDING_MODEL
            )
            return _normalize_openrouter_embedding_model(raw_model)

        raw_model = (
            embedding_model
            or _env_first("RAG_OPENAI_EMBEDDING_MODEL", "RAG_EMBEDDING_MODEL")
            or DEFAULT_OPENAI_EMBEDDING_MODEL
        )
        return _normalize_openai_embedding_model(raw_model)

    def _resolve_embedding_api_key(self, api_key: str | None) -> str | None:
        embedding_api_key = _secret_or_none(env_str("RAG_EMBEDDING_API_KEY"))
        if embedding_api_key:
            return embedding_api_key
        if self.embedding_provider == OPENROUTER_PROVIDER:
            return _secret_or_none(env_str("OPENROUTER_API_KEY"))
        openai_api_key = _secret_or_none(env_str("OPENAI_API_KEY"))
        if openai_api_key:
            return openai_api_key
        if self.provider == OPENAI_PROVIDER and self.embedding_provider == OPENAI_PROVIDER:
            return _secret_or_none(api_key)
        return None

    def _resolve_embedding_provider(self) -> str:
        configured = _normalize_embedding_provider(env_str("RAG_EMBEDDING_PROVIDER", ""))
        if configured:
            return configured
        if _env_first("RAG_OPENROUTER_EMBEDDING_MODEL"):
            return OPENROUTER_PROVIDER
        if not _secret_or_none(env_str("OPENAI_API_KEY")) and _secret_or_none(
            env_str("OPENROUTER_API_KEY")
        ):
            return OPENROUTER_PROVIDER
        return OPENAI_PROVIDER

    def _resolve_embedding_base_url(self) -> str:
        if self.embedding_provider == OPENROUTER_PROVIDER:
            configured = _env_first("RAG_OPENROUTER_EMBEDDING_BASE_URL")
            if configured:
                return configured
            generic = _env_first("RAG_EMBEDDING_BASE_URL")
            if "openrouter" in generic.lower():
                return generic
            return _env_first("RAG_OPENROUTER_BASE_URL") or DEFAULT_OPENROUTER_BASE_URL
        return _env_first("RAG_EMBEDDING_BASE_URL", "OPENAI_BASE_URL")

    def _build_embedding_client(self):
        embedding_client_kwargs: dict[str, Any] = {"api_key": self.embedding_api_key}
        if self.embedding_base_url:
            embedding_client_kwargs["base_url"] = self.embedding_base_url
        if self.embedding_provider == OPENROUTER_PROVIDER:
            headers = _openrouter_headers()
            if headers:
                embedding_client_kwargs["default_headers"] = headers
        return OpenAI(**embedding_client_kwargs)

    def _switch_embedding_client_to_openrouter(self) -> bool:
        if self.embedding_provider == OPENROUTER_PROVIDER:
            return False
        openrouter_key = _secret_or_none(env_str("OPENROUTER_API_KEY"))
        if not openrouter_key:
            return False
        self.embedding_provider = OPENROUTER_PROVIDER
        self.embedding_api_key = openrouter_key
        raw_model = (
            _env_first("RAG_OPENROUTER_EMBEDDING_MODEL")
            or _env_first("RAG_EMBEDDING_MODEL")
            or DEFAULT_OPENROUTER_EMBEDDING_MODEL
        )
        self.embedding_model = _normalize_openrouter_embedding_model(raw_model)
        self.embedding_base_url = self._resolve_embedding_base_url()
        self.embedding_client = self._build_embedding_client()
        self.embedding_cache_namespace = self.embedding_base_url or self.embedding_provider
        print(
            "[WARNING] RAG embedding provider switched to OpenRouter: "
            f"model={self.embedding_model}"
        )
        return True

    def _resolve_base_url(self, base_url: str | None) -> str:
        if base_url is not None:
            return base_url.strip()
        configured = _env_first("RAG_BASE_URL")
        if configured:
            return configured
        if self.provider == OPENROUTER_PROVIDER:
            return _env_first("RAG_OPENROUTER_BASE_URL") or DEFAULT_OPENROUTER_BASE_URL
        return _env_first("OPENAI_BASE_URL")

    def chunk_text(self, text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
        paragraphs = text.split("\n\n")
        chunks: list[str] = []
        current_chunk = ""

        for paragraph in paragraphs:
            paragraph = paragraph.strip()
            if not paragraph:
                continue

            if len(current_chunk) + len(paragraph) + 2 <= chunk_size:
                current_chunk = f"{current_chunk}\n\n{paragraph}" if current_chunk else paragraph
                continue

            if current_chunk:
                chunks.append(current_chunk)

            if len(paragraph) <= chunk_size:
                current_chunk = paragraph
                continue

            words = paragraph.split()
            temp_chunk = ""
            for word in words:
                if len(temp_chunk) + len(word) + 1 <= chunk_size:
                    temp_chunk = f"{temp_chunk} {word}" if temp_chunk else word
                else:
                    if temp_chunk:
                        chunks.append(temp_chunk)
                    temp_chunk = word
            current_chunk = temp_chunk

        if current_chunk:
            chunks.append(current_chunk)

        if overlap > 0 and len(chunks) > 1:
            overlapped_chunks = [chunks[0]]
            for index in range(1, len(chunks)):
                previous_chunk = chunks[index - 1]
                previous_tail = (
                    previous_chunk[-overlap:] if len(previous_chunk) > overlap else previous_chunk
                )
                overlapped_chunks.append(f"{previous_tail}\n\n{chunks[index]}")
            chunks = overlapped_chunks

        return chunks

    def get_embedding(self, text: str) -> list[float] | None:
        embeddings = self.get_embeddings([text])
        return embeddings[0] if embeddings else None

    def get_embeddings(self, texts: list[str], batch_size: int = 96) -> list[list[float]]:
        embeddings: list[list[float]] = []
        for start in range(0, len(texts), batch_size):
            batch = texts[start : start + batch_size]
            if not batch:
                continue
            try:
                response = self.embedding_client.embeddings.create(
                    model=self.embedding_model,
                    input=batch,
                )
            except Exception as exc:  # noqa: BLE001
                if self._switch_embedding_client_to_openrouter():
                    try:
                        response = self.embedding_client.embeddings.create(
                            model=self.embedding_model,
                            input=batch,
                        )
                    except Exception as retry_exc:  # noqa: BLE001
                        print(f"[ERROR] RAG OpenRouter embedding batch failed: {retry_exc}")
                        return []
                else:
                    print(f"[ERROR] RAG embedding batch failed: {exc}")
                    return []
            embeddings.extend(item.embedding for item in response.data)
        return embeddings

    @classmethod
    def _cache_key(
        cls,
        file_path: Path,
        provider: str,
        embedding_model: str,
    ) -> tuple[str, int, int, str, str]:
        stat = file_path.stat()
        return (
            str(file_path.resolve()),
            stat.st_mtime_ns,
            stat.st_size,
            provider,
            embedding_model,
        )

    @classmethod
    def _clone_documents(cls, documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [dict(document) for document in documents]

    @classmethod
    def _remember_document(
        cls,
        cache_key: tuple[str, int, int, str, str],
        documents: list[dict[str, Any]],
    ) -> None:
        with cls._cache_lock:
            if len(cls._document_cache) >= cls._max_cached_documents:
                oldest_key = next(iter(cls._document_cache))
                cls._document_cache.pop(oldest_key, None)
            cls._document_cache[cache_key] = cls._clone_documents(documents)

    def load_document(self, file_path: Path) -> None:
        cache_key = self._cache_key(
            file_path,
            self.embedding_cache_namespace,
            self.embedding_model,
        )
        content = file_path.read_text(encoding="utf-8")
        self.source_texts[str(file_path)] = content

        with self._cache_lock:
            cached_documents = self._document_cache.get(cache_key)
        if cached_documents is not None:
            self.documents.extend(self._clone_documents(cached_documents))
            print(f"[RAG] document cache hit: {file_path.name}, chunks: {len(cached_documents)}")
            return

        try:
            chunks = self.chunk_text(content)
            print(f"[RAG] loading document: {file_path.name}, chunks: {len(chunks)}")

            embeddings = self.get_embeddings(chunks)
            loaded_documents: list[dict[str, Any]] = []
            for index, (chunk, embedding) in enumerate(zip(chunks, embeddings, strict=False)):
                loaded_documents.append(
                    {
                        "text": chunk,
                        "embedding": np.array(embedding),
                        "source": str(file_path),
                        "chunk_id": index,
                    }
                )

            self.documents.extend(loaded_documents)
            store_cache_key = self._cache_key(
                file_path,
                self.embedding_cache_namespace,
                self.embedding_model,
            )
            self._remember_document(store_cache_key, loaded_documents)
            print(f"[RAG] document embedding complete: {len(loaded_documents)} chunks")
        except Exception as exc:
            print(f"[ERROR] RAG document load failed: {exc}")
            raise

    @staticmethod
    def _cosine_similarity(left: np.ndarray, right: np.ndarray) -> float:
        denominator = np.linalg.norm(left) * np.linalg.norm(right)
        if denominator == 0:
            return 0.0
        return float(np.dot(left, right) / denominator)

    @staticmethod
    def _elapsed_ms(started_at: float) -> float:
        return round((perf_counter() - started_at) * 1000, 3)

    @staticmethod
    def _public_retrieval_chunk(chunk: dict[str, Any]) -> dict[str, Any]:
        text = str(chunk.get("text") or "")
        source = str(chunk.get("source") or "")
        score = chunk.get("score")
        return {
            "rank": int(chunk.get("rank") or 0),
            "score": round(float(score), 6) if score is not None else None,
            "source": source,
            "fileName": Path(source).name if source else "",
            "chunkId": chunk.get("chunk_id"),
            "textPreview": text[:500],
        }

    def _public_retrieval_report(self, report: dict[str, Any]) -> dict[str, Any]:
        public_report = dict(report)
        public_report["chunks"] = [
            self._public_retrieval_chunk(chunk) for chunk in report.get("chunks", [])
        ]
        return public_report

    def retrieve(self, query: str, top_k: int = 3) -> dict[str, Any]:
        total_started = perf_counter()
        retrieval_report: dict[str, Any] = {
            "mode": "semantic",
            "topK": top_k,
            "candidateChunks": len(self.documents),
            "returnedChunks": 0,
            "embeddingModel": self.embedding_model,
            "timingMs": {
                "embedding": 0.0,
                "similarity": 0.0,
                "total": 0.0,
            },
            "chunks": [],
        }

        if not self.documents:
            retrieval_report["reason"] = "no_documents"
            retrieval_report["timingMs"]["total"] = self._elapsed_ms(total_started)
            return retrieval_report

        embedding_started = perf_counter()
        query_embedding = self.get_embedding(query)
        retrieval_report["timingMs"]["embedding"] = self._elapsed_ms(embedding_started)
        if not query_embedding:
            retrieval_report["reason"] = "embedding_failed"
            retrieval_report["timingMs"]["total"] = self._elapsed_ms(total_started)
            return retrieval_report

        query_vector = np.array(query_embedding)
        similarity_started = perf_counter()
        similarities = [
            (self._cosine_similarity(query_vector, document["embedding"]), document)
            for document in self.documents
        ]
        similarities.sort(key=lambda item: item[0], reverse=True)
        retrieval_report["timingMs"]["similarity"] = self._elapsed_ms(similarity_started)

        ranked_chunks: list[dict[str, Any]] = []
        for rank, (score, document) in enumerate(similarities[:top_k], start=1):
            ranked_document = dict(document)
            ranked_document["score"] = score
            ranked_document["rank"] = rank
            ranked_chunks.append(ranked_document)

        retrieval_report["chunks"] = ranked_chunks
        retrieval_report["returnedChunks"] = len(ranked_chunks)
        retrieval_report["timingMs"]["total"] = self._elapsed_ms(total_started)
        return retrieval_report

    def search_relevant_chunks(self, query: str, top_k: int = 3) -> list[dict[str, Any]]:
        return list(self.retrieve(query, top_k=top_k).get("chunks", []))

    def _is_exhaustive_question(self, question: str) -> bool:
        normalized = question.lower()
        return any(marker in normalized for marker in self._exhaustive_markers)

    def _build_exhaustive_chunks(self) -> list[dict[str, Any]]:
        source_texts = self.source_texts
        if not source_texts:
            grouped: dict[str, list[str]] = {}
            for document in self.documents:
                grouped.setdefault(str(document["source"]), []).append(str(document["text"]))
            source_texts = {source: "\n\n".join(chunks) for source, chunks in grouped.items()}

        exhaustive_chunks: list[dict[str, Any]] = []
        for source, text in source_texts.items():
            chunks = self.chunk_text(
                text,
                chunk_size=self.exhaustive_chunk_size,
                overlap=self.exhaustive_chunk_overlap,
            )
            for index, chunk in enumerate(chunks, start=1):
                exhaustive_chunks.append(
                    {
                        "source": source,
                        "chunk_id": index,
                        "text": chunk,
                    }
                )

        if self.exhaustive_max_chunks and len(exhaustive_chunks) > self.exhaustive_max_chunks:
            print(
                "[RAG] exhaustive chunk limit applied: "
                f"{self.exhaustive_max_chunks}/{len(exhaustive_chunks)}"
            )
            return exhaustive_chunks[: self.exhaustive_max_chunks]
        return exhaustive_chunks

    def _extract_exhaustive_chunk(
        self,
        question: str,
        chunk: dict[str, Any],
        chunk_index: int,
        total_chunks: int,
    ) -> str:
        prompt = f"""아래 문서 일부만 보고 사용자 요청에 답하는 데 필요한 항목과 사실을 모두 추출하세요.

[사용자 요청]
{question}

[문서 일부]
출처: {chunk["source"]}
청크: {chunk_index}/{total_chunks}
{chunk["text"]}

[규칙]
1. 이 청크에 명시된 내용만 사용하세요.
2. 이 청크에 관련 내용이 없으면 "없음"만 답하세요.
3. 목록 요청이면 한 줄에 하나씩 적으세요.
4. 원문 표기를 유지하고 추측으로 보완하지 마세요.

부분 결과:"""

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": "You extract only facts explicitly present in the provided document chunk.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_tokens=1200,
        )
        return (response.choices[0].message.content or "").strip()

    @staticmethod
    def _has_extracted_content(text: str) -> bool:
        normalized = text.strip().lower()
        return bool(normalized) and normalized not in {"없음", "none", "n/a", "not found"}

    def _answer_exhaustive_question(
        self,
        question: str,
        exhaustive_chunks: list[dict[str, Any]] | None = None,
    ) -> str:
        if exhaustive_chunks is None:
            exhaustive_chunks = self._build_exhaustive_chunks()
        if not exhaustive_chunks:
            return "문서에서 확인할 수 있는 내용이 없습니다."

        print(f"[RAG] exhaustive mode enabled: chunks={len(exhaustive_chunks)}")
        partial_results: list[str] = []
        total_chunks = len(exhaustive_chunks)

        try:
            for index, chunk in enumerate(exhaustive_chunks, start=1):
                extracted = self._extract_exhaustive_chunk(
                    question,
                    chunk,
                    chunk_index=index,
                    total_chunks=total_chunks,
                )
                if self._has_extracted_content(extracted):
                    partial_results.append(
                        f"[{Path(str(chunk['source'])).name} #{chunk['chunk_id']}]\n{extracted}"
                    )

            if not partial_results:
                return "문서에서 질문과 관련된 내용을 찾지 못했습니다."

            limit_note = ""
            if self.exhaustive_max_chunks and total_chunks >= self.exhaustive_max_chunks:
                limit_note = (
                    "\n주의: RAG_EXHAUSTIVE_MAX_CHUNKS 설정 때문에 "
                    f"처음 {self.exhaustive_max_chunks}개 청크만 처리했습니다."
                )

            merged_context = "\n\n".join(partial_results)
            final_prompt = f"""아래 부분 추출 결과를 병합해서 사용자 질문에 대한 최종 답변을 작성하세요.

[사용자 질문]
{question}

[부분 추출 결과]
{merged_context}

[규칙]
1. 중복 항목은 제거하세요.
2. 문서에 있는 내용만 사용하고 추측하지 마세요.
3. 목록 질문이면 빠진 항목이 없도록 목록으로 답하세요.
4. 답변은 한국어로 작성하세요.
{limit_note}

최종 답변:"""
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": "You merge extracted document facts without adding unsupported information.",
                    },
                    {"role": "user", "content": final_prompt},
                ],
                temperature=0.0,
                max_tokens=2500,
            )
            answer = (response.choices[0].message.content or "").strip()
            if limit_note and limit_note.strip() not in answer:
                answer = f"{answer}\n\n{limit_note.strip()}"
            return answer
        except Exception as exc:  # noqa: BLE001
            print(f"[ERROR] RAG exhaustive answer generation failed: {exc}")
            return f"답변 생성 중 오류가 발생했습니다: {exc}"

    def answer_question(self, question: str, top_k: int = 3) -> str:
        return str(self.answer_question_with_retrieval(question, top_k=top_k)["answer"])

    def answer_question_with_retrieval(self, question: str, top_k: int = 3) -> dict[str, Any]:
        if not self.documents and not self.source_texts:
            return {
                "answer": "문서를 먼저 로드해야 답변할 수 있습니다.",
                "retrieval": self._public_retrieval_report(
                    {
                        "mode": "none",
                        "topK": top_k,
                        "candidateChunks": 0,
                        "returnedChunks": 0,
                        "embeddingModel": self.embedding_model,
                        "timingMs": {"embedding": 0.0, "similarity": 0.0, "total": 0.0},
                        "chunks": [],
                        "reason": "no_loaded_documents",
                    }
                ),
            }

        current_sources = {document["source"] for document in self.documents} or set(
            self.source_texts
        )
        print(
            f"[RAG] loaded sources: {len(current_sources)} files, "
            f"chunks: {len(self.documents)}"
        )

        if self._is_exhaustive_question(question):
            retrieval_started = perf_counter()
            exhaustive_chunks = self._build_exhaustive_chunks()
            retrieval_report = {
                "mode": "exhaustive",
                "topK": top_k,
                "candidateChunks": len(self.documents),
                "returnedChunks": 0,
                "exhaustiveChunks": len(exhaustive_chunks),
                "embeddingModel": None,
                "timingMs": {
                    "embedding": 0.0,
                    "similarity": 0.0,
                    "total": self._elapsed_ms(retrieval_started),
                },
                "chunks": [],
            }
            return {
                "answer": self._answer_exhaustive_question(question, exhaustive_chunks),
                "retrieval": self._public_retrieval_report(retrieval_report),
            }

        if not self.documents:
            return {
                "answer": "문서 검색 인덱스를 만들지 못해 관련 정보를 찾을 수 없습니다.",
                "retrieval": self._public_retrieval_report(
                    {
                        "mode": "semantic",
                        "topK": top_k,
                        "candidateChunks": 0,
                        "returnedChunks": 0,
                        "embeddingModel": self.embedding_model,
                        "timingMs": {"embedding": 0.0, "similarity": 0.0, "total": 0.0},
                        "chunks": [],
                        "reason": "no_search_index",
                    }
                ),
            }

        retrieval_report = self.retrieve(question, top_k=top_k)
        relevant_chunks = list(retrieval_report.get("chunks", []))
        if not relevant_chunks:
            return {
                "answer": "문서에서 관련 정보를 찾을 수 없습니다.",
                "retrieval": self._public_retrieval_report(retrieval_report),
            }

        context = "\n\n".join(
            "[문서 {index} | 출처={source} | 청크={chunk_id} | 점수={score:.4f}]\n{text}".format(
                index=index,
                source=Path(str(chunk.get("source") or "")).name,
                chunk_id=chunk.get("chunk_id"),
                score=float(chunk.get("score") or 0.0),
                text=chunk["text"],
            )
            for index, chunk in enumerate(relevant_chunks, start=1)
        )
        prompt = f"""다음 문서 내용을 기반으로 사용자의 질문에 답하세요.

[문서 내용]
{context}

[사용자 질문]
{question}

[답변 규칙]
1. 문서 내용에 기반해서 정확하게 답하세요.
2. 문서에 없는 내용은 추측하지 마세요.
3. 답변은 한국어로 작성하세요.
4. 가능하면 구체적인 수치와 예시를 포함하세요.

답변:"""

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": "You answer questions using only the supplied document context.",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=1000,
            )
            return {
                "answer": response.choices[0].message.content or "",
                "retrieval": self._public_retrieval_report(retrieval_report),
            }
        except Exception as exc:  # noqa: BLE001
            print(f"[ERROR] RAG answer generation failed: {exc}")
            return {
                "answer": f"답변 생성 중 오류가 발생했습니다: {exc}",
                "retrieval": self._public_retrieval_report(retrieval_report),
            }

    def clear_documents(self) -> None:
        self.documents = []
        self.source_texts = {}
        print("[RAG] document store cleared")
