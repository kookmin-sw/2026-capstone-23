from core.env import env_str
from core.onprem_validation import validate_onprem_runtime


if __name__ == "__main__":
    validate_onprem_runtime()
    mode = env_str("WORKER_MODE", "all", strip=True).lower()
    if mode == "qwen_doc":
        from worker.qwen_doc_worker import run_qwen_doc_worker

        run_qwen_doc_worker()
    elif mode == "qwen_infer":
        from worker.qwen_infer_worker import run_qwen_infer_worker

        run_qwen_infer_worker()
    elif mode == "qwen_finalize":
        from worker.qwen_finalize_worker import run_qwen_finalize_worker

        run_qwen_finalize_worker()
    else:
        from worker.runtime import run_worker_from_env

        run_worker_from_env()
