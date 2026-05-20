# OpenAI Multi-Worker Setup

This project supports OpenAI-backed async jobs through the `openai` queue route.

## Recommended baseline

- OpenAI worker replicas: `2`
- Per-worker concurrency: `2`
- Total in-flight items: `4`

This is a safer starting point than one large worker because it gives better fault isolation and keeps scaling simple.

## Local development

Local defaults are kept in `.env.local`.

- `ENABLE_INLINE_EXEC_WORKER=0`
- `ENABLE_INLINE_RECOVERY_WORKER=0`
- `LOCAL_API_PORT=8001`
- `LOCAL_OPENAI_WORKER_COUNT=2`
- `LOCAL_OPENAI_WORKER_CONCURRENCY=2`
- `RABBITMQ_QUEUE=jobs.queue.local`
- `REDIS_URL=redis://localhost:6379/1`
- `STORE_SQLITE_PATH=data/app.local.db`

Start:

```bash
cd backend
bash shell/start_local_openai_stack.sh
```

Stop:

```bash
cd backend
bash shell/stop_local_openai_stack.sh
```

Open logs in an extra terminal:

```bash
cd backend
bash shell/open_local_openai_logs.sh
```

Split API / recovery / worker logs into separate terminals:

```bash
cd backend
bash shell/open_local_openai_logs.sh --split
```

Override worker count or concurrency:

```bash
cd backend
bash shell/start_local_openai_stack.sh --worker-count 3 --worker-concurrency 2 --port 8001
```

## Docker

Docker defaults are kept in `.env`.

- `OPENAI_WORKER_REPLICAS=2`
- `OPENAI_WORKER_MAX_CONCURRENCY=2`

Start:

```bash
cd backend
bash shell/start_docker_openai_stack.sh
```

Stop:

```bash
cd backend
bash shell/stop_docker_openai_stack.sh
```

Override replicas or concurrency:

```bash
cd backend
bash shell/start_docker_openai_stack.sh --worker-replicas 3 --worker-concurrency 2
```

## Notes

- Local and Docker queues are intentionally separated.
- Local uses `jobs.queue.local`; Docker uses `jobs.queue`.
- This avoids RabbitMQ orphan-message collisions between local and container processes.
- Job-level `parallelism` is metadata only; real throughput is controlled by worker replica count and `WORKER_MAX_CONCURRENCY`.
- Bash scripts are native entrypoints now; they do not rely on PowerShell.
- Local bash start uses `PYTHONIOENCODING=utf-8` and `PYTHONUTF8=1` so Korean logs stay readable in Git Bash.
