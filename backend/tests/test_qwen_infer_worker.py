from worker.qwen_infer_worker import _resolve_infer_worker_settings


def test_resolve_infer_worker_settings_prefers_qwen_specific_env(monkeypatch) -> None:
    monkeypatch.setenv("WORKER_MAX_CONCURRENCY", "1")
    monkeypatch.setenv("QWEN_WORKER_MAX_CONCURRENCY", "3")
    monkeypatch.setenv("QWEN_INFER_WORKER_MAX_CONCURRENCY", "4")
    monkeypatch.setenv("GPU_MAX_CONCURRENT_INFERENCE", "1")
    monkeypatch.setenv("QWEN_INFER_GPU_SLOTS", "2")

    max_concurrency, gpu_slots = _resolve_infer_worker_settings()

    assert max_concurrency == 4
    assert gpu_slots == 2


def test_resolve_infer_worker_settings_falls_back_to_generic_env(monkeypatch) -> None:
    for name in (
        "QWEN_WORKER_MAX_CONCURRENCY",
        "QWEN_INFER_WORKER_MAX_CONCURRENCY",
        "QWEN_INFER_GPU_SLOTS",
    ):
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setenv("WORKER_MAX_CONCURRENCY", "5")
    monkeypatch.setenv("GPU_MAX_CONCURRENT_INFERENCE", "3")

    max_concurrency, gpu_slots = _resolve_infer_worker_settings()

    assert max_concurrency == 5
    assert gpu_slots == 3
