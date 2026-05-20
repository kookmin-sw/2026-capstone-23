from eval.metrics.retrieval import compute_retrieval_metrics


def test_compute_retrieval_metrics_exact_chunk_match():
    metrics = compute_retrieval_metrics(
        [
            {"rank": 1, "source": "/tmp/doc-a.txt", "chunkId": 4},
            {"rank": 2, "source": "/tmp/doc-b.txt", "chunkId": 1},
        ],
        [{"source": "/data/doc-b.txt", "chunkId": 1}],
        k=2,
    )

    assert metrics == {
        "k": 2,
        "hit@k": 1.0,
        "recall@k": 1.0,
        "mrr@k": 0.5,
        "matchedExpected": 1,
        "expected": 1,
    }


def test_compute_retrieval_metrics_source_level_recall():
    metrics = compute_retrieval_metrics(
        [
            {"rank": 1, "source": "/tmp/doc-a.txt", "chunkId": 0},
            {"rank": 2, "source": "/tmp/doc-b.txt", "chunkId": 3},
        ],
        [
            {"source": "/gold/doc-a.txt"},
            {"source": "/gold/doc-c.txt"},
        ],
        k=2,
    )

    assert metrics["hit@k"] == 1.0
    assert metrics["recall@k"] == 0.5
    assert metrics["mrr@k"] == 1.0
    assert metrics["matchedExpected"] == 1
