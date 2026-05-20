from __future__ import annotations

from pathlib import Path
from typing import Any


def _value(item: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in item:
            return item[name]
    return None


def _source_key(value: Any) -> str:
    source = str(value or "").strip()
    if not source:
        return ""
    return Path(source).name


def _matches_expected(retrieved: dict[str, Any], expected: dict[str, Any]) -> bool:
    expected_source = _source_key(_value(expected, "source", "path"))
    retrieved_source = _source_key(_value(retrieved, "source", "path"))
    if expected_source and retrieved_source != expected_source:
        return False

    expected_chunk_id = _value(expected, "chunkId", "chunk_id")
    if expected_chunk_id is None:
        return bool(expected_source)

    retrieved_chunk_id = _value(retrieved, "chunkId", "chunk_id")
    return str(retrieved_chunk_id) == str(expected_chunk_id)


def compute_retrieval_metrics(
    retrieved_chunks: list[dict[str, Any]],
    expected_chunks: list[dict[str, Any]],
    *,
    k: int | None = None,
) -> dict[str, Any]:
    """Compute basic RAG retrieval metrics for one query.

    `expected_chunks` should contain `source`/`path` and optionally `chunkId`.
    If `chunkId` is omitted, matching is source-level.
    """
    limit = k if k is not None else len(retrieved_chunks)
    limited_retrieved = retrieved_chunks[: max(limit, 0)]
    expected_count = len(expected_chunks)
    if expected_count == 0:
        return {
            "k": limit,
            "hit@k": 0.0,
            "recall@k": 0.0,
            "mrr@k": 0.0,
            "matchedExpected": 0,
            "expected": 0,
        }

    matched_expected_indexes: set[int] = set()
    first_relevant_rank: int | None = None

    for retrieved_index, retrieved in enumerate(limited_retrieved, start=1):
        for expected_index, expected in enumerate(expected_chunks):
            if expected_index in matched_expected_indexes:
                continue
            if not _matches_expected(retrieved, expected):
                continue
            matched_expected_indexes.add(expected_index)
            if first_relevant_rank is None:
                first_relevant_rank = int(retrieved.get("rank") or retrieved_index)
            break

    matched_count = len(matched_expected_indexes)
    return {
        "k": limit,
        "hit@k": 1.0 if matched_count > 0 else 0.0,
        "recall@k": round(matched_count / expected_count, 6),
        "mrr@k": round(1 / first_relevant_rank, 6) if first_relevant_rank else 0.0,
        "matchedExpected": matched_count,
        "expected": expected_count,
    }
