from core.qwen_staged_pipeline import cleanup_staged_artifacts


def test_cleanup_staged_artifacts_removes_only_target_item_dir(tmp_path) -> None:
    target_images = tmp_path / "qwen_staged" / "ji_1" / "images"
    target_images.mkdir(parents=True)
    (target_images / "page_0001_img.png").write_bytes(b"image")

    sibling_images = tmp_path / "qwen_staged" / "ji_2" / "images"
    sibling_images.mkdir(parents=True)
    (sibling_images / "page_0001_img.png").write_bytes(b"image")

    assert cleanup_staged_artifacts(tmp_root=tmp_path, job_item_id="ji_1") is True
    assert not (tmp_path / "qwen_staged" / "ji_1").exists()
    assert (sibling_images / "page_0001_img.png").exists()


def test_cleanup_staged_artifacts_rejects_empty_or_traversal_item_id(tmp_path) -> None:
    root = tmp_path / "qwen_staged"
    root.mkdir()

    assert cleanup_staged_artifacts(tmp_root=tmp_path, job_item_id="") is False
    assert cleanup_staged_artifacts(tmp_root=tmp_path, job_item_id="../outside") is False
    assert root.exists()
