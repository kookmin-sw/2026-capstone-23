# Qwen Local GPU Server

This document covers the staged Qwen worker topology for a local physical GPU server.

Important runtime rule:

- Redis is the shared runtime state for `jobs`, `job_items`, `documents`, and all staged Qwen task namespaces.
- SQLite remains a write-through mirror on each node, but separate API/GPU servers must point to the same Redis and RabbitMQ.
- Recovery for staged Qwen is lease/heartbeat based, not document-duration based.

## Topology

- `qwen_doc`: CPU-side document preprocessing
- `qwen_infer`: long-lived GPU inference worker with Qwen loaded once
- `qwen_finalize`: final merge and output write

Queue flow:

```text
/parser/jobs -> qwen_doc -> qwen_infer -> qwen_finalize
```

`modelId=m3` now routes to `qwen_doc`.

## Recommended baseline for RTX 3090 + Qwen2.5-VL-7B

- `LOCAL_QWEN_WORKER_COUNT=2`
- `LOCAL_QWEN_WORKER_CONCURRENCY=1`
- `LOCAL_QWEN_GPU_SLOTS=1`
- `VLM_DEVICE=gpu`
- `QWEN_VL_7B_MODEL_PATH=/absolute/path/to/Qwen2.5-VL-7B-Instruct`

Interpretation:

- `LOCAL_QWEN_WORKER_COUNT` = number of `qwen_infer` GPU replicas
- `LOCAL_QWEN_WORKER_CONCURRENCY` = per-replica worker concurrency
- `LOCAL_QWEN_GPU_SLOTS` = in-process Qwen semaphore

On RTX 3090, keep both concurrency and GPU slots at `1` first.
Increase throughput by adding more `qwen_infer` replicas only if VRAM remains stable.

## Example `.env.local` on the GPU server

```env
DEFAULT_AUTO_EXECUTION_BACKEND=qwen_gpu

QUEUE_BACKEND=rabbitmq
STORE_BACKEND=sqlite
STATUS_CACHE_BACKEND=redis
STATUS_CACHE_NAMESPACES=jobs,job_items,job_events,documents,document_cache,qwen_preprocess_tasks,qwen_infer_tasks,qwen_infer_results,qwen_finalize_tasks,worker_leases

RABBITMQ_URL=amqp://guest:guest@localhost:5672/%2F
RABBITMQ_QUEUE=jobs.queue.local
REDIS_URL=redis://localhost:6379/1
STORE_SQLITE_PATH=data/app.local.db

LOCAL_QWEN_API_PORT=8002
LOCAL_QWEN_WORKER_COUNT=2
LOCAL_QWEN_WORKER_CONCURRENCY=1
LOCAL_QWEN_GPU_SLOTS=1
QWEN_WARMUP_ON_START=1
QWEN_INFER_MAX_RETRIES=2
QWEN_FINALIZE_MAX_RETRIES=2
WORKER_MISSING_STATE_MAX_RETRIES=5

JOB_ITEM_TIMEOUT_SECONDS=300
QWEN_WORKER_LEASE_TTL_SECONDS=3600
QWEN_WORKER_HEARTBEAT_INTERVAL_SECONDS=30

VLM_DEVICE=gpu
QWEN_VL_7B_MODEL_PATH=/srv/models/Qwen2.5-VL-7B-Instruct
QWEN_VISION_MIN_PIXELS=100352
QWEN_VISION_MAX_PIXELS=702464
QWEN_TABLE_MAX_TOKENS=512
QWEN_FLOWCHART_MAX_TOKENS=384
QWEN_MATH_MAX_TOKENS=384
QWEN_IMAGE_MAX_TOKENS=384
QWEN_RETRY_MAX_TOKENS=768
QWEN_GENERATE_MAX_TIME=90

INPUT_ROOT=data/inputs
OUTPUT_ROOT=data/outputs
TMP_ROOT=data/tmp
```

The values above are lightweight RTX 3090 defaults. Raise `QWEN_VISION_MAX_PIXELS`
or the token budgets only when dense tables lose too much detail.

## Start

```bash
cd backend
bash shell/start_local_qwen_stack.sh
```

Override values explicitly:

```bash
cd backend
bash shell/start_local_qwen_stack.sh --port 8002 --worker-count 2 --worker-concurrency 1 --gpu-slots 1
```

## Stop

```bash
cd backend
bash shell/stop_local_qwen_stack.sh
```

## Logs

```bash
cd backend
bash shell/open_local_qwen_logs.sh
```

Log files:

- `data/tmp/qwen-api.log`
- `data/tmp/qwen-recovery.log`
- `data/tmp/qwen-doc-worker.log`
- `data/tmp/qwen-finalize-worker.log`
- `data/tmp/qwen-infer-worker-1.log`
- `data/tmp/qwen-infer-worker-2.log`

## Queue behavior

- API still receives `/api/v1/parser/jobs`
- `modelId=m3` or `executionBackend=qwen_gpu` routes into `qwen_doc`
- `qwen_doc` produces inference tasks into `qwen_infer`
- each `qwen_infer` worker loads Qwen once and processes GPU tasks
- `qwen_finalize` merges inference results and writes final output
- recovery intervenes only when worker lease expires or stage invariants are broken

## Legacy local GPU worker

`WORKER_MODE=qwen_gpu` still exists for the legacy monolithic local-model path.
It remains useful for temporary local-model fallback, but `m3` uses the staged path now.
