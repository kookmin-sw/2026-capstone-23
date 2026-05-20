# Worker Routing

This project now supports route-aware async job execution for `/api/v1/parser/jobs`.

## Request fields

- `executionBackend=auto|openai|qwen_gpu`

## Stored job metadata

- `requestedExecutionBackend`
- `executionBackend`
- `queueRoute`

## Queue routes

- `openai`
- `qwen_gpu`
- `qwen_doc`
- `qwen_infer`
- `qwen_finalize`

## Default behavior

- `auto` resolves by model provider
- OpenAI-backed models route to `openai`
- Local GPU-backed models route to `qwen_gpu`
- Explicit incompatible combinations are rejected

## CPU-only deployment

Run the API:

```bash
QUEUE_BACKEND=rabbitmq \
STORE_BACKEND=sqlite \
STATUS_CACHE_BACKEND=redis \
ENABLE_INLINE_EXEC_WORKER=0 \
uvicorn api:app --host 0.0.0.0 --port 8000
```

Run the OpenAI worker:

```bash
QUEUE_BACKEND=rabbitmq \
STORE_BACKEND=sqlite \
STATUS_CACHE_BACKEND=redis \
WORKER_MODE=openai \
WORKER_MAX_CONCURRENCY=8 \
python -m worker.main
```

## GPU deployment for Qwen

Run the staged Qwen workers on the GPU node:

```bash
QUEUE_BACKEND=rabbitmq \
STORE_BACKEND=sqlite \
STATUS_CACHE_BACKEND=redis \
WORKER_MODE=qwen_doc \
python -m worker.main
```

```bash
QUEUE_BACKEND=rabbitmq \
STORE_BACKEND=sqlite \
STATUS_CACHE_BACKEND=redis \
WORKER_MODE=qwen_infer \
WORKER_MAX_CONCURRENCY=1 \
VLM_DEVICE=gpu \
GPU_MAX_CONCURRENT_INFERENCE=1 \
python -m worker.main
```

```bash
QUEUE_BACKEND=rabbitmq \
STORE_BACKEND=sqlite \
STATUS_CACHE_BACKEND=redis \
WORKER_MODE=qwen_finalize \
python -m worker.main
```

## Notes

- `worker.main` is the common worker entrypoint
- Persistent storage is SQLite; Redis is used as a write-through status cache for `jobs`, `job_items`, `job_events`, `documents`, and `document_cache`
- `WORKER_MODE=openai` consumes only the `openai` queue route
- `WORKER_MODE=qwen_gpu` consumes only the legacy `qwen_gpu` queue route
- `WORKER_MODE=qwen_doc` consumes only `qwen_doc`
- `WORKER_MODE=qwen_infer` consumes only `qwen_infer`
- `WORKER_MODE=qwen_finalize` consumes only `qwen_finalize`
- `WORKER_MODE=all` consumes `openai` and legacy `qwen_gpu` in round-robin order
- For RTX 3090 + `qwen2.5-vl-7b`, keep `WORKER_MAX_CONCURRENCY=1` and `GPU_MAX_CONCURRENT_INFERENCE=1` per infer worker as the safe baseline
- Increase Qwen throughput by adding GPU workers or GPU nodes, not by sharing one model instance across many threads
- For OpenAI multi-worker startup on Windows, see `docs/OPENAI_MULTI_WORKER.md`
- For local physical-server Qwen startup, see `docs/QWEN_LOCAL_SERVER.md`
