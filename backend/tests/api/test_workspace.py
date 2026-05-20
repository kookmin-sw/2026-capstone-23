import importlib
import json
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("OPENAI_API_KEY", "test-key")

from infra.store import ADMIN_SETTINGS


def make_client(tmp_path: Path) -> TestClient:
    deps_module = types.ModuleType("api.dependencies")
    deps_module.config = SimpleNamespace(
        input_root=tmp_path / "inputs",
        output_root=tmp_path / "outputs",
        tmp_root=tmp_path / "tmp",
    )
    deps_module.pipeline = SimpleNamespace(
        update_vlm_model=lambda *_args, **_kwargs: None,
        process_file=lambda *_args, **_kwargs: None,
    )
    deps_module.get_auto_processor = lambda: SimpleNamespace(
        start=lambda: True,
        stop=lambda: None,
        trigger_now=lambda *_args, **_kwargs: 0,
        get_status=lambda: {"scheduler_running": False, "processing": False},
    )
    deps_module.get_config = lambda: deps_module.config
    deps_module.set_runtime_services = lambda **_kwargs: None

    original_dependencies = sys.modules.get("api.dependencies")
    try:
        sys.modules["api.dependencies"] = deps_module
        import api.routers.workspace as workspace_module

        workspace_module = importlib.reload(workspace_module)
    finally:
        if original_dependencies is None:
            sys.modules.pop("api.dependencies", None)
        else:
            sys.modules["api.dependencies"] = original_dependencies

    app = FastAPI()
    app.include_router(workspace_module.router, prefix="/api/v1")
    return TestClient(app)


def setup_function():
    ADMIN_SETTINGS.clear()


def teardown_function():
    ADMIN_SETTINGS.clear()


def test_workspace_file_listing_preview_and_delete(tmp_path: Path):
    client = make_client(tmp_path)

    input_file = tmp_path / "inputs" / "incoming" / "sample.pdf"
    input_file.parent.mkdir(parents=True, exist_ok=True)
    input_file.write_bytes(b"%PDF-1.7")

    output_file = tmp_path / "outputs" / "incoming" / "sample.txt"
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(
        "원본 파일: sample.pdf\n"
        "페이지 수: 1\n"
        "------------------------------------------------------------\n\n"
        "[[TABLE]]<table><tr><td>1</td></tr></table>[[/TABLE]]\n"
        "[[TABLE_MARKDOWN]]# TableTitle: 표 제목\n- Row=1, Col=A: 1[[/TABLE_MARKDOWN]]\n"
        "[[IMAGE]]이미지 설명[[/IMAGE]]\n",
        encoding="utf-8",
    )
    meta_path = output_file.with_suffix(".meta.json")
    meta_path.write_text(json.dumps({"pageCount": 1}), encoding="utf-8")

    list_res = client.get("/api/v1/workspace/files", params={"scope": "output"})
    assert list_res.status_code == 200
    items = list_res.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["relativePath"] == "incoming/sample.txt"

    preview_res = client.get("/api/v1/workspace/files/content", params={"path": str(output_file)})
    assert preview_res.status_code == 200
    preview = preview_res.json()["data"]
    assert preview["hasHtml"] is True
    assert "<table>" in preview["htmlSection"]
    assert "표 제목" in preview["markdownSection"]
    assert preview["imageSections"] == ["이미지 설명"]
    assert preview["meta"]["pageCount"] == 1

    delete_res = client.post("/api/v1/workspace/files/delete", json={"paths": [str(output_file)]})
    assert delete_res.status_code == 200
    deleted = delete_res.json()["data"]
    assert deleted["deletedCount"] == 1
    assert output_file.exists() is False
    assert meta_path.exists() is False


def test_workspace_download_multiple_files_returns_zip(tmp_path: Path):
    client = make_client(tmp_path)

    first = tmp_path / "inputs" / "a.pdf"
    second = tmp_path / "outputs" / "b.txt"
    first.parent.mkdir(parents=True, exist_ok=True)
    second.parent.mkdir(parents=True, exist_ok=True)
    first.write_bytes(b"a")
    second.write_text("b", encoding="utf-8")

    res = client.post(
        "/api/v1/workspace/files/download",
        json={"paths": [str(first), str(second)]},
    )
    assert res.status_code == 200
    assert res.content[:2] == b"PK"
