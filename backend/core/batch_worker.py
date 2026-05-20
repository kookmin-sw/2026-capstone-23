"""
백그라운드 배치 변환 워커 (앱/클라이언트 종료 후에도 서버에서 계속 실행)
사용: python -m core.batch_worker
"""
import sys
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))


from core.config import load_config
from core.pipeline import DocumentPipeline
from core.batch_state import get_batch_state_path, load_batch_state, save_batch_state, is_stop_requested, print_batch_completion_stats, remove_completed_input


def _run_parallel(config, pipeline, state, all_items, start_index, language, outputs, parallel, max_retries=1):
    """병렬 처리 (파일 단위만, 중간 페이지 재개 없음)."""
    lock = threading.Lock()
    next_index = [start_index]
    completed_count_holder = [start_index]
    failed_count_holder = [state.get("failed_count", 0)]
    total_attempts = max_retries + 1

    def process_one_item(idx):
        if idx >= len(all_items):
            return None
        item = all_items[idx]
        path_str = item.get("path", "")
        is_dir = item.get("is_dir", False)
        src = Path(path_str)
        if not src.exists():
            return (idx, [], False)
        paths_this = []
        for attempt in range(total_attempts):
            try:
                if is_dir:
                    results = pipeline.process_directory(src, language=language)
                    for result in results:
                        paths_this.append(str(Path(result).resolve()))
                else:
                    out = pipeline.process_file(src, language=language, resume_info=None)
                    paths_this.append(str(Path(out).resolve()))
                return (idx, paths_this, False)
            except Exception as e:
                print(f"[batch_worker] 처리 실패 {src} (시도 {attempt + 1}/{total_attempts}): {e}")
                if attempt < max_retries:
                    import time
                    time.sleep(60)
        return (idx, [], True)

    def worker():
        while True:
            with lock:
                if is_stop_requested(config):
                    break
                if next_index[0] >= len(all_items):
                    break
                idx = next_index[0]
                next_index[0] += 1
            idx_result = process_one_item(idx)
            if idx_result is None:
                continue
            idx, paths_this, failed = idx_result
            if not failed:
                item = all_items[idx]
                if not item.get("is_dir", False):
                    remove_completed_input(item["path"], config)
            with lock:
                for p in paths_this:
                    outputs.append(p)
                completed_count_holder[0] += 1
                if failed:
                    failed_count_holder[0] += 1
                s = load_batch_state(config) or {}
                s["completed_count"] = completed_count_holder[0]
                s["failed_count"] = failed_count_holder[0]
                s["last_outputs"] = list(outputs)
                s["status"] = "running"
                save_batch_state(config, s)
                print(f"[batch_worker] 진행: {completed_count_holder[0]}/{len(all_items)}")

    with ThreadPoolExecutor(max_workers=parallel) as executor:
        futures = [executor.submit(worker) for _ in range(parallel)]
        for f in as_completed(futures):
            try:
                f.result()
            except Exception as e:
                print(f"[batch_worker] 워커 예외: {e}")


def main():
    config = load_config()
    pipeline = DocumentPipeline(config)
    state_path = get_batch_state_path(config)

    if not state_path.exists():
        print("[batch_worker] 상태 파일이 없습니다. 종료합니다.")
        return

    state = load_batch_state(config)
    if not state:
        print("[batch_worker] 상태를 읽을 수 없습니다. 종료합니다.")
        return

    all_items = state.get("all_items", [])
    completed_count = state.get("completed_count", 0)
    language = state.get("language", "한국어")
    vlm_model = state.get("vlm_model", "openrouter/openai/gpt-5.2")

    if completed_count >= len(all_items):
        print("[batch_worker] 처리할 항목이 없습니다. 종료합니다.")
        state["status"] = "idle"
        save_batch_state(config, state)
        return

    pipeline.update_vlm_model(vlm_model)
    total = len(all_items)
    outputs = list(state.get("last_outputs", []))
    parallel = max(1, int(state.get("parallel", 1)))
    max_retries = max(0, min(3, int(state.get("max_retries", 1))))
    import time
    start_time = time.time()

    if parallel > 1:
        _run_parallel(config, pipeline, state, all_items, completed_count, language, outputs, parallel, max_retries=max_retries)
        state = load_batch_state(config) or {}
        state["status"] = "idle"
        state["stop_requested"] = False
        elapsed = time.time() - start_time
        state["completed_at"] = __import__("datetime").datetime.now().isoformat()
        state["total_elapsed_seconds"] = elapsed
        save_batch_state(config, state)
        print_batch_completion_stats(state, elapsed)
        return

    for i in range(completed_count, total):
        item = all_items[i]
        path_str = item.get("path", "")
        is_dir = item.get("is_dir", False)
        src = Path(path_str)

        if not src.exists():
            print(f"[batch_worker] 경로가 없습니다. 건너뜀: {path_str}")
            completed_count = i + 1
            state["completed_count"] = completed_count
            state["last_outputs"] = outputs
            state.pop("current_file_path", None)
            state.pop("current_page", None)
            state.pop("current_page_total", None)
            state.pop("current_output_path", None)
            save_batch_state(config, state)
            continue

        # 이어서 실행: 파일 중간에서 재개 (저장된 state 또는 출력 파일에서 복구)
        resume_info = None
        cur_path = state.get("current_file_path")
        cur_page = state.get("current_page", 0)
        cur_total = state.get("current_page_total", 0)
        cur_out = state.get("current_output_path")

        # 출력 파일이 이미 있으면 마지막 완료 페이지 추정 (이전 실행 중단 시 복구)
        if not is_dir and not cur_out:
            try:
                out_path = pipeline._relative_output_path(Path(path_str))
            except Exception:
                try:
                    rel = Path(path_str).relative_to(config.input_root)
                except ValueError:
                    rel = Path(Path(path_str).name)
                out_path = config.output_root / rel.parent / f"{Path(path_str).stem}.txt"
            if out_path.exists():
                try:
                    text = out_path.read_text(encoding="utf-8")
                    import re
                    pages = [int(m.group(1)) for m in re.finditer(r"## Page (\d+)", text)]
                    if pages:
                        cur_page = max(pages)
                        cur_out = str(out_path)
                        cur_path = path_str
                        # page_count는 PDF에서 확인해야 하므로 일단 cur_total=cur_page+1로 시작
                        cur_total = cur_page + 20  # 임시, 실제로는 pipeline에서 page_count 사용
                        state["current_file_path"] = cur_path
                        state["current_page"] = cur_page
                        state["current_output_path"] = cur_out
                        state["current_page_total"] = cur_total
                        save_batch_state(config, state)
                        print(f"[batch_worker] 출력 파일에서 복구: Page {cur_page}까지 완료, Page {cur_page + 1}부터 이어서")
                except Exception as ex:
                    print(f"[batch_worker] 출력 파일 파싱 실패: {ex}")

        if (
            not is_dir
            and cur_path == path_str
            and cur_page > 0
            and cur_out
            and Path(cur_out).exists()
        ):
            next_page = cur_page + 1
            resume_info = {
                "resume_from_page": next_page,
                "existing_output_path": cur_out,
            }
            print(f"[batch_worker] 이어서 실행: {path_str} Page {next_page}부터")

        def _make_progress_callback(_path_str, _i):
            def _cb(output_path, page_num, page_count):
                s = load_batch_state(config) or {}
                s["current_file_path"] = _path_str
                s["current_file_index"] = _i
                s["current_page"] = page_num
                s["current_page_total"] = page_count
                s["current_output_path"] = output_path
                s["completed_count"] = _i  # 아직 파일 미완료
                s["last_outputs"] = outputs
                s["status"] = "running"
                save_batch_state(config, s)
            return _cb

        retry_count = 0
        total_attempts = max_retries + 1
        success = False
        while retry_count < total_attempts:
            try:
                if is_dir:
                    results = pipeline.process_directory(src, language=language)
                    for result in results:
                        out_abs = Path(result).resolve()
                        outputs.append(str(out_abs))
                else:
                    out = pipeline.process_file(
                        src,
                        language=language,
                        progress_callback=_make_progress_callback(path_str, i),
                        resume_info=resume_info,
                    )
                    out_abs = Path(out).resolve()
                    outputs.append(str(out_abs))
                success = True
                break
            except Exception as e:
                retry_count += 1
                print(f"[batch_worker] 처리 실패 {src} (시도 {retry_count}/{total_attempts}): {e}")
                import traceback
                traceback.print_exc()
                if retry_count < total_attempts:
                    import time
                    time.sleep(60)  # 1분 대기 후 재시도

        if not success:
            print(f"[batch_worker] 파일 건너뜀 (재시도 실패): {path_str}")
            state["current_file_path"] = path_str
            state["current_file_index"] = i
            state["last_outputs"] = outputs
            save_batch_state(config, state)
            continue

        if not is_dir:
            remove_completed_input(src, config)

        completed_count = i + 1
        state["completed_count"] = completed_count
        state["last_outputs"] = outputs
        state["status"] = "running"
        state.pop("current_file_path", None)
        state.pop("current_page", None)
        state.pop("current_page_total", None)
        state.pop("current_output_path", None)
        save_batch_state(config, state)
        print(f"[batch_worker] 진행: {completed_count}/{total}")

    state["status"] = "idle"
    state["stop_requested"] = False
    elapsed = time.time() - start_time
    state["completed_at"] = __import__("datetime").datetime.now().isoformat()
    state["total_elapsed_seconds"] = elapsed
    save_batch_state(config, state)
    print_batch_completion_stats(state, elapsed)


if __name__ == "__main__":
    main()
