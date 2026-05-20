from __future__ import annotations

import os
from pathlib import Path


TESTS_ROOT = Path(__file__).resolve().parent
BACKEND_ROOT = TESTS_ROOT.parent
TEST_STORE_PATH = BACKEND_ROOT / "data" / "tmp" / f"pytest-store-{os.getpid()}.db"


os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("ENABLE_LOCAL_QWEN_MODEL", "1")
os.environ.setdefault("STATUS_CACHE_BACKEND", "none")
os.environ.setdefault("STORE_SQLITE_PATH", str(TEST_STORE_PATH))
