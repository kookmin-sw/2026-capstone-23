from pathlib import Path
from typing import Dict, Any, List
import os
import shutil
import math
import re
from tqdm import tqdm 
import hashlib
import io
from PIL import Image

from core.config import AppConfig
from core.converters import (
    convert_to_pdf, extract_images_from_hwp, extract_text_from_hwp_pyhwp,
    get_hwpx_image_page_mapping, _is_hwpx_format, SUPPORTED_DOC, ConversionError,
    parse_hwp_document_direct, parse_hwpx_document, parse_hwpx_tables, merge_hwpx_tables_into_elements,
)
from core.pdf_extractor import extract_pdf, _should_remove_text_for_table_region, _bbox_overlaps_any
from core.vlm_client import VLMClient
from core.output import build_document_text, write_outputs

try:
    from pdf2image import convert_from_path
    PDF2IMAGE_AVAILABLE = True
except ImportError:
    PDF2IMAGE_AVAILABLE = False


IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp"}

_STRUCTURED_HWP_BLOCK_PATTERN = re.compile(
    r"(\[\[(?:TABLE|TABLE_MARKDOWN|IMAGE)\]\].*?\[\[/(?:TABLE|TABLE_MARKDOWN|IMAGE)\]\])",
    re.DOTALL,
)
_HWP_TABLE_MARKER_PATTERN = re.compile(r"<\s*표\s*>")
_HWP_IMAGE_MARKER_PATTERN = re.compile(r"<\s*그림\s*>")


def split_hwp_source_text_into_pages(source_text: str, page_count: int) -> List[str]:
    if page_count <= 0:
        return []

    normalized = (source_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return [""] * page_count

    form_feed_pages = [part.strip() for part in normalized.split("\f") if part.strip()]
    if len(form_feed_pages) == page_count:
        return form_feed_pages

    lines = normalized.split("\n")
    lines_per_page = max(1, math.ceil(len(lines) / page_count))
    pages: List[str] = []
    for index in range(page_count):
        start = index * lines_per_page
        end = min(len(lines), start + lines_per_page)
        chunk = "\n".join(lines[start:end]).strip()
        pages.append(chunk)

    while len(pages) < page_count:
        pages.append("")
    return pages[:page_count]


def merge_hwp_source_text_with_structured_blocks(source_page_text: str, generated_page_text: str) -> str:
    source_text = (source_page_text or "").strip()
    generated_text = (generated_page_text or "").strip()

    if not source_text:
        return generated_text
    if not generated_text:
        return source_text

    blocks = [match.group(1).strip() for match in _STRUCTURED_HWP_BLOCK_PATTERN.finditer(generated_text)]
    if not blocks:
        return source_text

    merged_text = source_text
    remaining_blocks: List[str] = []

    for block in blocks:
        marker_pattern = _HWP_IMAGE_MARKER_PATTERN if block.startswith("[[IMAGE]]") else _HWP_TABLE_MARKER_PATTERN
        merged_text, replaced_count = marker_pattern.subn(block, merged_text, count=1)
        if replaced_count == 0:
            remaining_blocks.append(block)

    if remaining_blocks:
        merged_text = "\n\n".join(part for part in [merged_text, "\n\n".join(remaining_blocks)] if part)

    merged_text = re.sub(r"\n{3,}", "\n\n", merged_text)
    return merged_text.strip()


class DocumentPipeline:
    class IncompleteConversionError(RuntimeError):
        """Raised when a document did not finish converting all pages."""
        pass

    def __init__(self, config: AppConfig):
        self.config = config
        self.vlm = VLMClient(openai_model=config.openai_model, device=config.vlm_device, max_concurrent_api=config.vlm_max_concurrent, gpu_max_concurrent=config.gpu_max_concurrent)
    
    def update_vlm_model(self, model_name: str):
        """VLM 모델을 동적으로 변경"""
        print(f"[DEBUG] VLM 모델 변경: {self.config.openai_model} -> {model_name}")

        # 로컬 모델 (Qwen2.5-VL) 또는 OpenRouter 선택 시 VLMClient 재생성 필요
        needs_recreate = (
            model_name.lower().startswith("qwen2.5-vl") or
            model_name.lower().startswith("openrouter/")
        )
        if needs_recreate:
            print(f"[DEBUG] 전용 모델 선택됨 ({model_name}) - VLMClient 재생성")
            try:
                self.vlm = VLMClient(openai_model=model_name, device=self.config.vlm_device, max_concurrent_api=self.config.vlm_max_concurrent, gpu_max_concurrent=self.config.gpu_max_concurrent)
                print(f"[DEBUG] VLMClient 재생성 완료 (use_qwen={self.vlm.use_qwen}, use_openrouter={self.vlm.use_openrouter})")
            except Exception as e:
                print(f"[ERROR] VLMClient 재생성 실패: {e}")
                import traceback
                traceback.print_exc()
                raise
        else:
            # 기존 API 모델은 service의 모델명만 변경
            if hasattr(self.vlm, 'service') and hasattr(self.vlm.service, 'openai_model'):
                # OpenRouter/Qwen에서 OpenAI 모델로 전환 시 재생성
                if getattr(self.vlm, 'use_openrouter', False) or getattr(self.vlm, 'use_qwen', False):
                    print(f"[DEBUG] 전용 모델에서 {model_name}로 변경 - VLMClient 재생성")
                    self.vlm = VLMClient(openai_model=model_name, device=self.config.vlm_device, max_concurrent_api=self.config.vlm_max_concurrent, gpu_max_concurrent=self.config.gpu_max_concurrent)
                else:
                    self.vlm.service.openai_model = model_name
            elif hasattr(self.vlm, 'use_qwen') and self.vlm.use_qwen:
                print(f"[DEBUG] Qwen에서 {model_name}로 변경 - VLMClient 재생성")
                self.vlm = VLMClient(openai_model=model_name, device=self.config.vlm_device, max_concurrent_api=self.config.vlm_max_concurrent, gpu_max_concurrent=self.config.gpu_max_concurrent)

        # config도 업데이트
        self.config.openai_model = model_name

    def _to_pdf(self, path: Path) -> Path:
        ext = path.suffix.lower()
        if ext == ".pdf":
            return path
        if ext in SUPPORTED_DOC:
            return convert_to_pdf(path, self.config.tmp_root)
        raise ValueError(f"지원하지 않는 포맷: {path}")

    def _process_excel(
        self,
        path: Path,
        language: str = "한국어",
        output_base_dir: Path = None,
        progress_callback=None,
    ) -> Path:
        """Excel 파일 처리 — A/B 파트 결합 후 output.py로 전달.

        A 파트(parse_excel): 전체 문서 구조(시트별 페이지 + HTML 표)
        B 파트(parse_excel_tables): 스타일 기반 헤더 인식 표 → meta.json 보강
        """
        from core.converters import parse_excel, parse_excel_tables
        from core.spreadsheet_table_extractors import _parse_xlsx_combined

        pdf_result = None
        excel_tables = []
        used_combined_excel_parser = False
        if path.suffix.lower() == ".xlsx":
            try:
                pdf_result, excel_tables = _parse_xlsx_combined(path)
                used_combined_excel_parser = True
            except Exception as e:
                print(f"[WARNING] combined Excel parsing failed, falling back: {e}")

        # A 파트: 시트별 페이지 구조 + HTML 표 생성
        if pdf_result is None:
            pdf_result = parse_excel(path)

        # B 파트: 스타일 기반 헤더 인식 표 추출 (meta 보강용)
        if not used_combined_excel_parser and not excel_tables:
            try:
                excel_tables = parse_excel_tables(path)
            except Exception as e:
                print(f"[WARNING] parse_excel_tables 실패, meta에서 생략: {e}")
                excel_tables = []

        content = build_document_text(
            source_path=path,
            page_count=pdf_result["page_count"],
            pages=pdf_result["pages"],
            image_results={},
        )

        meta = {
            "source": str(path),
            "page_count": pdf_result["page_count"],
            "images": [],
            "tables": [
                {"html": t["html"], "first_line": t["first_line"]}
                for t in excel_tables
            ],
        }

        if output_base_dir is not None:
            output_path = self._relative_output_path(path, base_dir=output_base_dir)
        else:
            output_path = self._relative_output_path(path)
        write_outputs(output_path, content, meta)

        if progress_callback:
            progress_callback(str(output_path), pdf_result["page_count"], pdf_result["page_count"])

        return output_path

    def _process_hwpx_direct(
        self,
        path: Path,
        language: str = "한국어",
        output_base_dir: Path = None,
        progress_callback=None,
    ) -> Path:
        """HWPX는 ZIP/XML 구조를 직접 파싱하고, 내장 이미지만 VLM으로 보강한다."""
        parsed = parse_hwpx_document(path)
        all_images = [
            img
            for page in parsed.get("pages", [])
            for img in page.get("images", [])
        ]
        image_results: Dict[str, Any] = {}
        if all_images:
            print(f"[DEBUG] HWPX 내장 이미지 {len(all_images)}개 VLM 분석 시작")
            from concurrent.futures import ThreadPoolExecutor, as_completed

            def _analyze_hwpx_image(image):
                image_bytes = image.get("bytes")
                image_id = image.get("image_id", "")
                if not (image_bytes and image_id):
                    return image_id, {"text": "", "error": "missing bytes or id"}
                try:
                    result = self.vlm.describe_image(
                        image_bytes,
                        language=language,
                        is_table=None,
                        is_flowchart=None,
                        is_math=None,
                    )
                    return image_id, result
                except Exception as exc:  # noqa: BLE001
                    print(f"[WARNING] HWPX 이미지 {image_id} VLM 분석 실패: {exc}")
                    return image_id, {"text": "", "error": str(exc)}

            is_local_gpu = getattr(self.vlm, "use_qwen", False) or getattr(self.vlm, "use_deepseek", False)
            concurrency = self.config.gpu_max_concurrent if is_local_gpu else self.config.vlm_max_concurrent
            workers = max(1, min(len(all_images), concurrency))
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(_analyze_hwpx_image, image): image
                    for image in all_images
                }
                for future in tqdm(as_completed(futures), total=len(all_images), desc="HWPX 이미지 VLM 분석"):
                    image_id, result = future.result()
                    if image_id:
                        image_results[image_id] = result
            print(f"[DEBUG] HWPX 이미지 VLM 분석 완료: {len(image_results)}개 결과")
        content = build_document_text(
            source_path=path,
            page_count=parsed["page_count"],
            pages=parsed["pages"],
            image_results=image_results,
        )
        meta = {
            "source": str(path),
            "page_count": parsed["page_count"],
            "pipeline": "hwpx_direct_xml",
            "images": [
                {
                    "image_id": img.get("image_id", ""),
                    "page": img.get("page", 1),
                    "bbox": img.get("bbox", [0, 0, 0, 0]),
                    "sha1": img.get("sha1", ""),
                    "vlm": image_results.get(img.get("image_id", ""), {}),
                }
                for img in all_images
            ],
            "tables": [
                {
                    "html": table.get("html", ""),
                    "markdown": table.get("markdown", ""),
                    "first_line": table.get("first_line", ""),
                }
                for table in parsed.get("tables", [])
            ],
        }

        if output_base_dir is not None:
            output_path = self._relative_output_path(path, base_dir=output_base_dir)
        else:
            output_path = self._relative_output_path(path)
        write_outputs(output_path, content, meta)

        if progress_callback:
            progress_callback(str(output_path), parsed["page_count"], parsed["page_count"])

        return output_path

    def _process_hwp_direct(
        self,
        path: Path,
        language: str = "한국어",
        output_base_dir: Path = None,
        progress_callback=None,
    ) -> Path:
        """HWP는 hwp5html 우선으로 직접 추출하고, 내장 이미지만 VLM으로 보강한다."""
        parsed = parse_hwp_document_direct(path, tmp_dir=self.config.tmp_root)
        all_images = [
            img
            for page in parsed.get("pages", [])
            for img in page.get("images", [])
        ]
        image_results: Dict[str, Any] = {}
        if all_images:
            print(f"[DEBUG] HWP 내장 이미지 {len(all_images)}개 VLM 분석 시작")
            from concurrent.futures import ThreadPoolExecutor, as_completed

            def _analyze_hwp_image(image):
                image_bytes = image.get("bytes")
                image_id = image.get("image_id", "")
                if not (image_bytes and image_id):
                    return image_id, {"text": "", "error": "missing bytes or id"}
                try:
                    result = self.vlm.describe_image(
                        image_bytes,
                        language=language,
                        is_table=None,
                        is_flowchart=None,
                        is_math=None,
                    )
                    return image_id, result
                except Exception as exc:  # noqa: BLE001
                    print(f"[WARNING] HWP 이미지 {image_id} VLM 분석 실패: {exc}")
                    return image_id, {"text": "", "error": str(exc)}

            is_local_gpu = getattr(self.vlm, "use_qwen", False) or getattr(self.vlm, "use_deepseek", False)
            concurrency = self.config.gpu_max_concurrent if is_local_gpu else self.config.vlm_max_concurrent
            workers = max(1, min(len(all_images), concurrency))
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(_analyze_hwp_image, image): image
                    for image in all_images
                }
                for future in tqdm(as_completed(futures), total=len(all_images), desc="HWP 이미지 VLM 분석"):
                    image_id, result = future.result()
                    if image_id:
                        image_results[image_id] = result
            print(f"[DEBUG] HWP 이미지 VLM 분석 완료: {len(image_results)}개 결과")

        content = build_document_text(
            source_path=path,
            page_count=parsed["page_count"],
            pages=parsed["pages"],
            image_results=image_results,
        )
        meta = {
            "source": str(path),
            "page_count": parsed["page_count"],
            "pipeline": parsed.get("source_type", "hwp_direct"),
            "images": [
                {
                    "image_id": img.get("image_id", ""),
                    "page": img.get("page", 1),
                    "bbox": img.get("bbox", [0, 0, 0, 0]),
                    "sha1": img.get("sha1", ""),
                    "vlm": image_results.get(img.get("image_id", ""), {}),
                }
                for img in all_images
            ],
            "tables": [],
        }

        if output_base_dir is not None:
            output_path = self._relative_output_path(path, base_dir=output_base_dir)
        else:
            output_path = self._relative_output_path(path)
        write_outputs(output_path, content, meta)

        if progress_callback:
            progress_callback(str(output_path), parsed["page_count"], parsed["page_count"])

        return output_path

    def _relative_output_path(self, src: Path, base_dir: Path = None) -> Path:
        """
        출력 경로 생성
        base_dir이 지정되면 해당 디렉토리를 기준으로 상대 경로 계산
        """
        if base_dir is None:
            base_dir = self.config.input_root
        
        try:
            rel = src.relative_to(base_dir)
        except ValueError:
            # base_dir의 하위가 아니면 파일명만 사용
            rel = Path(src.name)
        rel = Path(rel)
        return self.config.output_root / rel.parent / f"{rel.stem}.txt"
    
    def _render_pdf_pages_with_fitz(self, pdf_path: Path, dpi: int = 150) -> List["Image.Image"]:
        import fitz
        doc = fitz.open(pdf_path)
        images: List["Image.Image"] = []
        try:
            zoom = dpi / 72
            mat = fitz.Matrix(zoom, zoom)
            for page_index in range(len(doc)):
                page = doc.load_page(page_index)
                pix = page.get_pixmap(matrix=mat, alpha=False)
                mode = "RGB"
                if pix.alpha:
                    mode = "RGBA"
                img = Image.frombytes(mode, [pix.width, pix.height], pix.samples)
                if mode == "RGBA":
                    img = img.convert("RGB")
                images.append(img)
        finally:
            doc.close()
        return images
    
    def _cleanup_previous_results(self, src_path: Path) -> None:
        """
        동일한 파일의 이전 분석 결과를 삭제
        - data/outputs에서 .txt, .json 파일 삭제
        - data/tmp에서 관련 임시 파일 삭제
        """
        try:
            # 원본 파일의 상대 경로 계산
            try:
                rel = src_path.relative_to(self.config.input_root)
            except ValueError:
                rel = Path(src_path.name)
            
            rel = Path(rel)
            stem = rel.stem  # 확장자 제거한 파일명
            
            # 1. outputs 디렉토리에서 관련 파일 삭제
            output_txt = self.config.output_root / rel.parent / f"{stem}.txt"
            output_json = self.config.output_root / rel.parent / f"{stem}.json"
            output_meta = output_txt.with_suffix(".meta.json")  # write_outputs에서 생성하는 형식
            
            deleted_files = []
            for file_path in [output_txt, output_json, output_meta]:
                if file_path.exists():
                    file_path.unlink()
                    deleted_files.append(str(file_path))
            
            # 2. tmp 디렉토리에서 관련 파일/디렉토리 삭제 (동일 소스 재분석 시 이전 변환 결과 제거)
            # 배치 상태/로그는 삭제하지 않음
            tmp_skip_names = {"batch_state.json", "batch_worker.log"}
            tmp_files_to_delete = []

            def _collect_tmp_entries(dir_path: Path) -> None:
                if not dir_path.exists():
                    return
                for entry in dir_path.iterdir():
                    if entry.name in tmp_skip_names:
                        continue
                    tmp_stem = entry.stem
                    # 원본 파일명과 동일하거나, 원본 파일명으로 시작하는 경우 (예: stem.pdf, stem_html)
                    if stem == tmp_stem or tmp_stem.startswith(stem + "_") or tmp_stem.startswith(stem + "."):
                        tmp_files_to_delete.append(entry)

            # 같은 디렉토리 구조에서 찾기
            tmp_rel_dir = self.config.tmp_root / rel.parent
            _collect_tmp_entries(tmp_rel_dir)
            # 루트 tmp 디렉토리에서도 찾기 (convert_to_pdf는 tmp_root 직하위에 stem.pdf, stem_html 생성)
            if tmp_rel_dir != self.config.tmp_root:
                _collect_tmp_entries(self.config.tmp_root)

            # 중복 제거 후 삭제 (디렉토리는 rmtree, 파일은 unlink)
            for tmp_file in set(tmp_files_to_delete):
                if not tmp_file.exists():
                    continue
                try:
                    if tmp_file.is_dir():
                        shutil.rmtree(tmp_file)
                    else:
                        tmp_file.unlink()
                    deleted_files.append(str(tmp_file))
                except OSError as e:
                    print(f"[WARNING] tmp 삭제 실패 {tmp_file}: {e}")
            
            if deleted_files:
                print(f"[DEBUG] 이전 분석 결과 삭제: {len(deleted_files)}개 파일")
                for f in deleted_files[:5]:  # 처음 5개만 출력
                    print(f"  - {f}")
                if len(deleted_files) > 5:
                    print(f"  ... 외 {len(deleted_files) - 5}개")
        except Exception as e:
            print(f"[WARNING] 이전 분석 결과 삭제 중 오류 발생: {e}")
            # 오류가 발생해도 계속 진행
    
    def _process_hwp_page_by_page(
        self,
        hwp_path: Path,
        language: str = "한국어",
        output_base_dir: Path = None,
        progress_callback=None,
        resume_from_page: int = None,
        existing_output_path: Path = None,
    ) -> Path:
        """
        HWP 파일을 페이지 단위로 순차 처리
        1. PDF 변환 (표가 페이지 경계를 넘지 않도록 CSS 추가됨)
        2. 각 페이지를 독립적으로 처리하여 txt 파일에 순차적으로 추가
        progress_callback(output_path, page_num, page_count): 페이지 완료 시 호출 (배치 이어서 실행용)
        resume_from_page, existing_output_path: 이어서 실행 시 기존 파일에 append
        """
        import fitz
        
        # 1. PDF로 변환
        result = self._to_pdf(hwp_path)
        if isinstance(result, tuple):
            pdf_path, table_info = result
        else:
            pdf_path = result
            table_info = {"simple_tables": [], "complex_tables": []}
        
        print(f"[DEBUG] PDF 생성: {pdf_path}")
        print(f"[DEBUG] 표 분석: 간단 {len(table_info.get('simple_tables', []))}개, 복잡 {len(table_info.get('complex_tables', []))}개")
        
        # 2. PDF 페이지 수 확인
        doc = fitz.open(pdf_path)
        pdf_page_count = len(doc)
        doc.close()
        
        # 3. PDF 페이지를 이미지로 변환
        if PDF2IMAGE_AVAILABLE:
            try:
                pdf_page_images = convert_from_path(str(pdf_path), dpi=150)
                print(f"[DEBUG] PDF {len(pdf_page_images)}개 페이지를 이미지로 변환")
            except Exception as e:
                print(f"[WARNING] pdf2image 변환 실패, PyMuPDF로 폴백합니다: {e}")
                pdf_page_images = self._render_pdf_pages_with_fitz(pdf_path, dpi=150)
                print(f"[DEBUG] PyMuPDF로 PDF {len(pdf_page_images)}개 페이지를 이미지로 변환")
        else:
            print("[WARNING] pdf2image 미설치, PyMuPDF로 폴백합니다.")
            pdf_page_images = self._render_pdf_pages_with_fitz(pdf_path, dpi=150)
            print(f"[DEBUG] PyMuPDF로 PDF {len(pdf_page_images)}개 페이지를 이미지로 변환")
        
        if len(pdf_page_images) != pdf_page_count:
            print(f"[WARNING] pdf2image 결과({len(pdf_page_images)})와 실제 페이지({pdf_page_count})가 다릅니다. PyMuPDF로 재추출합니다.")
            pdf_page_images = self._render_pdf_pages_with_fitz(pdf_path, dpi=150)
        
        page_count = len(pdf_page_images)
        if page_count != pdf_page_count:
            raise self.IncompleteConversionError(
                f"PDF 페이지 렌더링 실패: 기대 {pdf_page_count}페이지, 실제 {page_count}페이지"
            )
        
        # 4. 간단한 표 정보 준비
        simple_tables = table_info.get("simple_tables", [])
        complex_tables = table_info.get("complex_tables", [])
        all_tables_ordered = []
        for idx, text in simple_tables:
            all_tables_ordered.append(("simple", idx, text))
        for idx, text, row_count in complex_tables:
            all_tables_ordered.append(("complex", idx, text, row_count))
        all_tables_ordered.sort(key=lambda x: x[1])
        
        # 5. 출력 파일 준비
        if output_base_dir is not None:
            output_path = self._relative_output_path(hwp_path, base_dir=output_base_dir)
        else:
            output_path = self._relative_output_path(hwp_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        # 6. 각 페이지를 순차적으로 처리
        global_table_idx = 0  # 전역 표 인덱스
        processed_pages = set()
        source_text_pages: List[str] = []
        try:
            hwp_source_text = extract_text_from_hwp_pyhwp(hwp_path)
            source_text_pages = split_hwp_source_text_into_pages(hwp_source_text, page_count)
            print(f"[DEBUG] HWP 원문 텍스트 추출 성공: {len(hwp_source_text)} chars")
        except Exception as e:
            print(f"[WARNING] HWP 원문 텍스트 추출 실패, PDF/OCR 결과만 사용: {e}")
        hwp_images = extract_images_from_hwp(hwp_path)
        img_cursor = 0  # HWP BinData 이미지 커서 (OLE2 HWP용)
        hwpx_image_mapping = {}  # HWPX용: {page_num: [image_idx, ...]}
        hwpx_tables = []
        hwpx_table_index = [0]
        if _is_hwpx_format(hwp_path):
            if hwp_images:
                hwpx_image_mapping = get_hwpx_image_page_mapping(
                    hwp_path, page_count, extracted_images=hwp_images
                )
                total_mapped = sum(len(v) for v in hwpx_image_mapping.values())
                print(f"[DEBUG] HWPX 이미지 {len(hwp_images)}개 추출, 페이지별 매핑 {total_mapped}개")
            hwpx_tables = parse_hwpx_tables(hwp_path)
        
        # 6-0. PDF 전체를 1회만 파싱 (페이지 루프 밖에서)
        from core.pdf_extractor import extract_pdf, detect_effective_pdf_pages
        pdf_data = extract_pdf(pdf_path)
        pdf_total_pages = len(pdf_data["pages"])
        print(f"[DEBUG] PDF 파싱 완료: {pdf_total_pages}페이지")
        
        # PDF 페이지에 텍스트가 거의 없거나 부분 이미지만 있는 경우 전체 페이지를 이미지로 처리
        import fitz
        doc = fitz.open(pdf_path)
        for page_idx in range(len(pdf_data["pages"])):
            page_data = pdf_data["pages"][page_idx]
            page_text = page_data.get("text", "").strip()
            page_images = page_data.get("images", [])

            if page_data.get("merged_table_bboxes"):
                print(f"[DEBUG] Page {page_idx+1}: 이전 페이지 표에 병합된 페이지 → 전체 페이지 렌더링 생략")
                continue
            
            # 조건: 텍스트가 200자 미만이고, 이미지가 없거나 부분 이미지만
            needs_fullpage = False
            
            if len(page_text) < 200:
                if len(page_images) == 0:
                    needs_fullpage = True
                    print(f"[DEBUG] Page {page_idx+1}: 텍스트 부족 & 이미지 없음 → 전체 페이지 변환")
                else:
                    # 이미지가 있지만 페이지의 일부만 차지하는지 확인
                    page = doc.load_page(page_idx)
                    page_width = page.rect.width
                    page_height = page.rect.height
                    page_area = page_width * page_height
                    
                    # 모든 이미지의 총 면적 계산
                    total_img_area = 0
                    for img in page_images:
                        bbox = img.get("bbox", [0, 0, 0, 0])
                        img_area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
                        total_img_area += img_area
                    
                    # 이미지가 페이지의 70% 미만을 차지하면 부분 이미지로 간주
                    if total_img_area < page_area * 0.7:
                        needs_fullpage = True
                        coverage = (total_img_area / page_area) * 100
                        print(f"[DEBUG] Page {page_idx+1}: 부분 이미지만 존재 (커버리지 {coverage:.1f}%) → 전체 페이지 변환")
            
            if needs_fullpage:
                # 페이지를 이미지로 렌더링
                page = doc.load_page(page_idx)
                pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))  # 2배 확대
                img_bytes = pix.tobytes("png")
                
                from PIL import Image as PILImage
                pil_img = PILImage.open(io.BytesIO(img_bytes))
                img_width, img_height = pil_img.size
                
                sha1 = hashlib.sha1(img_bytes).hexdigest()
                
                # 전체 페이지 이미지
                full_page_img = {
                    "image_id": f"page-{page_idx+1}-fullpage",
                    "page": page_idx + 1,
                    "bbox": [0, 0, img_width, img_height],
                    "bytes": img_bytes,
                    "sha1": sha1,
                    "is_table": True,  # 표로 간주
                }
                
                # images 리스트에 추가
                if "images" not in page_data:
                    page_data["images"] = []
                page_data["images"].insert(0, full_page_img)
                
                # elements에도 추가 (bytes 포함)
                if "elements" not in page_data:
                    page_data["elements"] = []
                page_data["elements"].insert(0, {
                    "type": "image",
                    "image_id": f"page-{page_idx+1}-fullpage",
                    "bbox": [0, 0, img_width, img_height],
                    "bytes": img_bytes,
                    "sha1": sha1,
                })
        
        doc.close()
        
        # 쓰레기 페이지 감지 (OLE/바이너리 덤프가 페이지로 렌더된 경우)
        effective_pages = detect_effective_pdf_pages(pdf_data["pages"])
        if effective_pages < pdf_total_pages:
            original_total = pdf_total_pages
            pdf_data["pages"] = pdf_data["pages"][:effective_pages]
            pdf_data["page_count"] = effective_pages
            pdf_page_images = pdf_page_images[:effective_pages]
            page_count = min(page_count, effective_pages)
            pdf_total_pages = effective_pages
            print(f"[INFO] 쓰레기 페이지 제거: {page_count}페이지만 처리 (원본 {original_total}페이지)")
        
        # 실제 PDF 페이지 수와 fitz/pdf2image 페이지 수 비교
        if pdf_total_pages != page_count:
            print(f"[WARNING] PDF 페이지 수 불일치: extract_pdf={pdf_total_pages}, fitz/pdf2image={page_count}")
            # 더 작은 값으로 통일하여 인덱스 오류 방지
            page_count = min(page_count, pdf_total_pages)
            print(f"[INFO] {page_count}페이지로 처리 진행")
        
        # 이어서 실행: 기존 파일에 append (헤더 건너뛰기)
        start_page = 1
        if resume_from_page is not None and existing_output_path is not None and existing_output_path.exists():
            output_path = existing_output_path
            start_page = resume_from_page
            print(f"[DEBUG] 이어서 실행: Page {start_page}부터 {page_count}까지 처리")
        else:
            # 헤더 작성 (쓰레기 페이지 감지 후 업데이트된 page_count 사용)
            header_lines = [
                f"원본 파일: {hwp_path}",
                f"페이지 수: {page_count}",
                "-" * 60,
                ""
            ]
            output_path.write_text("\n".join(header_lines), encoding='utf-8')
        
        for page_num in range(start_page, page_count + 1):
            print(f"\n[DEBUG] === Page {page_num}/{page_count} 처리 시작 ===")
            
            # 6-1. PDF에서 해당 페이지의 텍스트, 이미지, 표 영역 추출 (bbox 기반)
            page_pdf_data = pdf_data["pages"][page_num - 1]
            
            # 표 영역 찾기 (먼저 표 영역을 찾아야 텍스트 제거 가능)
            from core.converters import extract_table_regions_from_pdf_by_text
            table_regions = extract_table_regions_from_pdf_by_text(pdf_path, page_num, 10)

            # 네이티브 추출된 표 영역은 VLM 이미지 경로에서 제외
            native_table_bboxes_for_page = [
                e["bbox"] for e in page_pdf_data.get("elements", [])
                if e.get("type") == "native_table" and e.get("bbox")
            ]
            merged_table_bboxes_for_page = [
                bbox for bbox in page_pdf_data.get("merged_table_bboxes", [])
                if bbox
            ]
            table_bboxes_handled_by_native = native_table_bboxes_for_page + merged_table_bboxes_for_page
            if table_bboxes_handled_by_native:
                table_regions = [
                    r for r in table_regions
                    if not _bbox_overlaps_any(r, table_bboxes_handled_by_native)
                ]
            table_count = len(table_regions)

            # bbox 기반 요소 추출 (텍스트 + 네이티브 표)
            # native_table은 output.py가 인식하는 "table" 타입으로 정규화
            elements = []
            for e in page_pdf_data.get("elements", []):
                if e.get("type") == "text":
                    elements.append(e)
                elif e.get("type") == "native_table" and e.get("html"):
                    elements.append({
                        "type": "table",
                        "bbox": e["bbox"],
                        "content": e["html"],
                        "markdown": "",
                    })
            # HWPX: XML 표를 문서 순서대로 텍스트 블록과 매칭하여 [[TABLE]] 요소로 치환
            if hwpx_tables:
                elements = merge_hwpx_tables_into_elements(elements, hwpx_tables, hwpx_table_index)
            page_text = page_pdf_data.get("text", "")
            
            # PDF에서 추출한 이미지들 (표가 아닌 일반 이미지)
            pdf_page_images_list = []
            for elem in page_pdf_data.get("elements", []):
                if elem.get("type") == "image":
                    # 텍스트 기반 표 이미지는 제외 (별도로 크롭 처리됨)
                    if elem.get("is_text_table", False):
                        continue
                    
                    is_full_page = elem.get("is_full_page", False)
                    is_flowchart = elem.get("is_flowchart", False)

                    img_bytes = elem.get("bytes")
                    img_id = elem.get("image_id", "")
                    img_bbox = elem.get("bbox", [0, 0, 0, 0])
                    if img_bytes and img_id:
                        # 최소 bbox 크기 필터 (안전망 — pdf_extractor에서 1차 필터링 후 남은 것)
                        if not is_full_page:
                            img_w = img_bbox[2] - img_bbox[0]
                            img_h = img_bbox[3] - img_bbox[1]
                            if img_w < 50 or img_h < 50:
                                continue

                        # 전체 페이지 이미지는 항상 추가 (표 영역 겹침 체크 우회)
                        if is_full_page:
                            pdf_page_images_list.append({
                                "image_id": img_id,
                                "page": page_num,
                                "bbox": img_bbox,
                                "bytes": img_bytes,
                                "sha1": elem.get("sha1", ""),
                                "is_table": False,
                                "is_flowchart": is_flowchart,  # 흐름도 플래그 전달
                                "is_full_page": True
                            })
                            continue

                        # 표 영역과 겹치는지 확인 (표가 아닌 일반 이미지만 추가)
                        is_in_table = False
                        if table_regions:
                            img_x0, img_y0, img_x1, img_y1 = img_bbox
                            img_center_x = (img_x0 + img_x1) / 2
                            img_center_y = (img_y0 + img_y1) / 2
                            for table_x0, table_y0, table_x1, table_y1 in table_regions:
                                if (table_x0 <= img_center_x <= table_x1 and 
                                    table_y0 <= img_center_y <= table_y1):
                                    is_in_table = True
                                    break
                        
                        if not is_in_table:
                            pdf_page_images_list.append({
                                "image_id": img_id,
                                "page": page_num,
                                "bbox": img_bbox,
                                "bytes": img_bytes,
                                "sha1": elem.get("sha1", ""),
                                "is_table": False,  # 일반 이미지 (차트, 흐름도 등)
                            })
            
            print(f"[DEBUG] Page {page_num}: PDF에서 추출한 일반 이미지 {len(pdf_page_images_list)}개 (차트, 흐름도 등)")
            
            # 표 영역과 겹치는 텍스트 블록 제거 (중복 방지)
            if table_regions or table_bboxes_handled_by_native:
                filtered_elements = []
                removed_text_count = 0
                page_width = float(page_pdf_data.get("width") or 0)
                page_height = float(page_pdf_data.get("height") or 0)
                for elem in elements:
                    if elem.get("type") != "text":
                        filtered_elements.append(elem)
                        continue

                    elem_bbox = elem.get("bbox", [0, 0, 0, 0])
                    bx0, by0, bx1, by1 = elem_bbox
                    center_x = (bx0 + bx1) / 2
                    center_y = (by0 + by1) / 2

                    # native_table: find_tables() 기반으로 신뢰도 높음 → center-point 포함만 확인
                    in_native = any(
                        nx0 <= center_x <= nx1 and ny0 <= center_y <= ny1
                        for nx0, ny0, nx1, ny1 in table_bboxes_handled_by_native
                    )
                    # VLM 표 영역: 기존 보수적 검사 (전페이지 오검출 방어)
                    in_vlm = not in_native and any(
                        _should_remove_text_for_table_region(
                            elem_bbox, [tx0, ty0, tx1, ty1],
                            page_width, page_height, elem.get("content", ""),
                        )
                        for tx0, ty0, tx1, ty1 in table_regions
                    )

                    if in_native or in_vlm:
                        removed_text_count += 1
                    else:
                        filtered_elements.append(elem)

                elements = filtered_elements
                print(
                    f"[DEBUG] Page {page_num}: 표 내부 텍스트 {removed_text_count}개 제거 후 "
                    f"{len(elements)}개 텍스트 요소 유지"
                )
            
            # 6-2. 해당 페이지의 <그림> 개수 확인 (HWP BinData 이미지)
            if hwpx_image_mapping:
                # HWPX: section0.xml 기반 페이지별 매핑 사용
                figure_indices = hwpx_image_mapping.get(page_num, [])
            else:
                # OLE2 HWP: 기존 방식 (첫 페이지만 또는 HTML의 <그림> 태그 기반)
                figure_indices = []
                if page_num == 1 and img_cursor < len(hwp_images):
                    figure_indices = [img_cursor]  # 첫 페이지만 1개
            
            print(f"[DEBUG] Page {page_num}: 표 {table_count}개, <그림> {len(figure_indices)}개, 텍스트 요소 {len(elements)}개")
            
            # 6-3. 해당 페이지의 이미지 준비
            page_images = []
            
            # PDF에서 추출한 일반 이미지 추가 (차트, 흐름도 등)
            page_images.extend(pdf_page_images_list)
            
            # <그림>용 이미지 (HWP BinData)
            for img_idx in figure_indices:
                if 0 <= img_idx < len(hwp_images):
                    img_copy = dict(hwp_images[img_idx])
                    img_copy["page"] = page_num
                    img_copy["image_id"] = img_copy.get("image_id") or f"hwp-img-p{page_num}-{img_idx+1}"
                    page_images.append(img_copy)
            if not hwpx_image_mapping and figure_indices:
                img_cursor += len(figure_indices)

            def _is_table_like_elements(_elements: List[Dict[str, Any]]) -> bool:
                if len(_elements) < 8:
                    return False
                xs = []
                ys = []
                for elem in _elements:
                    bbox = elem.get("bbox", [0, 0, 0, 0])
                    x0, y0, x1, y1 = bbox
                    xs.append((x0 + x1) / 2)
                    ys.append((y0 + y1) / 2)
                xs.sort()
                ys.sort()
                def _cluster_count(vals, tol):
                    if not vals:
                        return 0
                    count = 1
                    last = vals[0]
                    for v in vals[1:]:
                        if abs(v - last) > tol:
                            count += 1
                            last = v
                    return count
                col_clusters = _cluster_count(xs, 20)
                row_clusters = _cluster_count(ys, 12)
                return col_clusters >= 2 and row_clusters >= 3

            if table_count == 0 and _is_table_like_elements(elements) and page_num <= len(pdf_page_images):
                pil_img = pdf_page_images[page_num - 1]
                img_width, img_height = pil_img.size
                img_buffer = io.BytesIO()
                pil_img.save(img_buffer, format='PNG')
                img_bytes = img_buffer.getvalue()
                sha1 = hashlib.sha1(img_bytes).hexdigest()
                page_images.append({
                    "image_id": f"hwp-table-g{global_table_idx+1}-p{page_num}-full",
                    "page": page_num,
                    "bbox": [0, 0, img_width, img_height],
                    "bytes": img_bytes,
                    "sha1": sha1,
                    "is_table": True,
                    "global_table_idx": global_table_idx,
                })
                global_table_idx += 1

            # 표 감지 실패 시(0개)에도 표 형태가 명확한 페이지는 전체 페이지를 표로 분석 (일반적 보정)
            def _is_table_like_elements(_elements):
                # 요소가 너무 적으면 표로 보기 어려움
                if len(_elements) < 8:
                    return False
                # x/y 중심 좌표 클러스터 수 계산
                xs = []
                ys = []
                for e in _elements:
                    bbox = e.get("bbox", [0, 0, 0, 0])
                    x0, y0, x1, y1 = bbox
                    xs.append((x0 + x1) / 2)
                    ys.append((y0 + y1) / 2)
                xs.sort()
                ys.sort()
                def _cluster_count(vals, tol):
                    if not vals:
                        return 0
                    count = 1
                    last = vals[0]
                    for v in vals[1:]:
                        if abs(v - last) > tol:
                            count += 1
                            last = v
                    return count
                # 2개 이상 열, 3개 이상 행이면 표 가능성 높음
                col_clusters = _cluster_count(xs, 20)
                row_clusters = _cluster_count(ys, 12)
                return col_clusters >= 2 and row_clusters >= 3

            if table_count == 0 and _is_table_like_elements(elements) and page_num <= len(pdf_page_images):
                pil_img = pdf_page_images[page_num - 1]
                img_width, img_height = pil_img.size
                img_buffer = io.BytesIO()
                pil_img.save(img_buffer, format='PNG')
                img_bytes = img_buffer.getvalue()
                sha1 = hashlib.sha1(img_bytes).hexdigest()
                page_images.append({
                    "image_id": f"hwp-table-g{global_table_idx+1}-p{page_num}-t1",
                    "page": page_num,
                    "bbox": [0, 0, img_width, img_height],
                    "bytes": img_bytes,
                    "sha1": sha1,
                    "is_table": True,
                    "global_table_idx": global_table_idx,
                })
                global_table_idx += 1
            
            # <표>용 이미지 (PDF 페이지 이미지에서 개별 크롭)
            if table_count > 0 and page_num <= len(pdf_page_images):
                pil_img = pdf_page_images[page_num - 1]
                img_width, img_height = pil_img.size
                
                from core.converters import extract_table_regions_from_pdf_by_text
                
                # PDF에서 표 영역 bbox 추출 시도
                table_regions = extract_table_regions_from_pdf_by_text(pdf_path, page_num, table_count)
                
                if table_regions and len(table_regions) >= table_count:
                    # 추출된 표 영역을 y 좌표로 정렬하여 순서 보장
                    table_regions_sorted = sorted(table_regions, key=lambda r: r[1])
                    print(f"[DEBUG] Page {page_num}: {len(table_regions_sorted)}개 표 영역 추출 성공")
                    
                    for table_idx, (x0, y0, x1, y1) in enumerate(table_regions_sorted[:table_count]):
                        scale = 150 / 72
                        crop_x0 = max(0, int(x0 * scale))
                        crop_y0 = max(0, int(y0 * scale))
                        crop_x1 = min(img_width, int(x1 * scale))
                        crop_y1 = min(img_height, int(y1 * scale))
                        
                        if crop_x1 <= crop_x0 or crop_y1 <= crop_y0:
                            continue
                        
                        cropped_img = pil_img.crop((crop_x0, crop_y0, crop_x1, crop_y1))
                        img_buffer = io.BytesIO()
                        cropped_img.save(img_buffer, format='PNG')
                        img_bytes = img_buffer.getvalue()
                        sha1 = hashlib.sha1(img_bytes).hexdigest()
                        
                        page_images.append({
                            "image_id": f"hwp-table-g{global_table_idx+1}-p{page_num}-t{table_idx+1}",
                            "page": page_num,
                            "bbox": [x0, y0, x1, y1],
                            "bytes": img_bytes,
                            "sha1": sha1,
                            "is_table": True,
                            "global_table_idx": global_table_idx,
                        })
                        global_table_idx += 1
                else:
                    # 균등 분할로 폴백
                    print(f"[WARNING] Page {page_num}: 표 영역 추출 실패, 균등 분할 방식으로 크롭")
                    y_step = img_height / table_count
                    margin_x = int(img_width * 0.03)
                    overlap = int(img_height * 0.05)
                    
                    for table_idx in range(table_count):
                        crop_y0 = max(0, int(table_idx * y_step - overlap))
                        if table_idx == 0:
                            crop_y0 = 0
                        crop_y1 = min(img_height, int((table_idx + 1) * y_step + overlap))
                        if table_idx == table_count - 1:
                            crop_y1 = img_height
                        
                        if crop_y1 <= crop_y0:
                            continue
                        
                        cropped_img = pil_img.crop((margin_x, crop_y0, img_width - margin_x, crop_y1))
                        img_buffer = io.BytesIO()
                        cropped_img.save(img_buffer, format='PNG')
                        img_bytes = img_buffer.getvalue()
                        sha1 = hashlib.sha1(img_bytes).hexdigest()
                        
                        page_images.append({
                            "image_id": f"hwp-table-g{global_table_idx+1}-p{page_num}-t{table_idx+1}",
                            "page": page_num,
                            "bbox": [margin_x, crop_y0, img_width - margin_x, crop_y1],
                            "bytes": img_bytes,
                            "sha1": sha1,
                            "is_table": True,
                            "global_table_idx": global_table_idx,
                        })
                        global_table_idx += 1
            
            # 6-4. VLM 분석 (이미지별 병렬 처리)
            image_results = {}
            if page_images:
                print(f"[DEBUG] Page {page_num}: {len(page_images)}개 이미지 VLM 분석 시작")
                from concurrent.futures import ThreadPoolExecutor, as_completed

                def _analyze_image_hwp(img):
                    """단일 이미지 VLM 분석 (병렬 실행용)"""
                    _img_bytes = img.get("bytes")
                    _img_id = img.get("image_id", "")
                    if not (_img_bytes and _img_id):
                        return _img_id, {"text": "", "error": "missing bytes or id"}
                    try:
                        _is_table = img.get("is_table", False) or img.get("is_text_table", False)
                        _is_flowchart = img.get("is_flowchart", None)
                        _result = self.vlm.describe_image(
                            _img_bytes,
                            language=language,
                            is_table=_is_table if _is_table else None,
                            is_flowchart=_is_flowchart,
                            is_math=None
                        )
                        return _img_id, _result
                    except Exception as _e:
                        print(f"[WARNING] 이미지 {_img_id} VLM 분석 실패: {_e}")
                        import traceback
                        traceback.print_exc()
                        return _img_id, {"text": "", "error": str(_e)}

                _is_local_gpu = getattr(self.vlm, 'use_qwen', False)
                _concurrency = self.config.gpu_max_concurrent if _is_local_gpu else self.config.vlm_max_concurrent
                _img_workers = min(len(page_images), _concurrency)
                with ThreadPoolExecutor(max_workers=_img_workers) as executor:
                    futures = {
                        executor.submit(_analyze_image_hwp, img): img
                        for img in page_images
                    }
                    for future in tqdm(as_completed(futures), total=len(page_images), desc=f"VLM 이미지 분석 (Page {page_num})"):
                        _img_id, _result = future.result()
                        if _img_id:
                            image_results[_img_id] = _result
                print(f"[DEBUG] Page {page_num}: VLM 분석 완료")
            
            # 6-5. 페이지 텍스트 생성 및 파일에 추가
            # bbox 기반 요소를 사용하여 텍스트와 표를 정렬
            # 표 이미지를 elements에 추가하여 bbox 기반 정렬
            all_elements = list(elements)  # 텍스트 요소 복사
            
            # 표 이미지를 elements에 추가 (bbox 기반 정렬을 위해)
            for img in page_images:
                img_id = img.get("image_id", "")
                img_bbox = img.get("bbox", [0, 0, 0, 0])
                if img_id:
                    all_elements.append({
                        "type": "image",
                        "image_id": img_id,
                        "bbox": img_bbox,
                    })
            
            # bbox 기반으로 정렬 (reading order)
            from core.pdf_extractor import _sort_by_reading_order
            sorted_elements = _sort_by_reading_order(all_elements)
            
            page_data_for_output = {
                "page": page_num,
                "elements": sorted_elements,
                "text": page_text,  # 호환성
                "images": page_images,  # 호환성
            }
            
            page_content = build_document_text(
                source_path=hwp_path,
                page_count=page_count,
                pages=[page_data_for_output],
                image_results=image_results,
            )
            
            # 페이지 내용만 추출 (헤더 제외)
            page_lines = page_content.split('\n')
            # "## Page X" 이후의 내용만 추출
            page_start_idx = 0
            for i, line in enumerate(page_lines):
                if line.startswith("## Page"):
                    page_start_idx = i + 1
                    break
            
            page_text_content = '\n'.join(page_lines[page_start_idx:])
            if source_text_pages:
                source_page_text = source_text_pages[page_num - 1] if page_num - 1 < len(source_text_pages) else ""
                page_text_content = merge_hwp_source_text_with_structured_blocks(source_page_text, page_text_content)
            
            # 파일에 추가 (append mode) 및 즉시 디스크 반영
            with open(output_path, 'a', encoding='utf-8') as f:
                f.write(f"\n## Page {page_num}\n")
                f.write(page_text_content)
                f.write("\n")
                f.flush()
                try:
                    os.fsync(f.fileno())
                except (OSError, AttributeError):
                    pass
            print(f"[DEBUG] Page {page_num} 처리 완료 및 파일에 추가")
            if progress_callback:
                progress_callback(str(output_path), page_num, page_count)
            processed_pages.add(page_num)
        
        if len(processed_pages) != page_count or max(processed_pages, default=0) != page_count:
            raise self.IncompleteConversionError(
                f"문서 변환 미완료: {len(processed_pages)}/{page_count}페이지"
            )
        
        return output_path

    def process_file(
        self,
        path: Path,
        language: str = "한국어",
        output_base_dir: Path = None,
        progress_callback=None,
        resume_info: dict = None,
    ) -> Path:
        path = path.resolve()
        
        # 이어서 실행이 아니면 이전 분석 결과 삭제
        if not resume_info:
            self._cleanup_previous_results(path)
        
        ext = path.suffix.lower()

        if ext in IMAGE_EXT:
            img_bytes = path.read_bytes()
            sha1 = hashlib.sha1(img_bytes).hexdigest()
            
            # 이미지 크기 확인 (bbox 설정용)
            from PIL import Image
            pil_img = Image.open(io.BytesIO(img_bytes))
            img_width, img_height = pil_img.size
            
            pdf_result = {
                "page_count": 1,
                "pages": [
                    {
                        "page": 1,
                        "text": "",
                        "images": [
                            {
                                "image_id": "page-1-img-1",
                                "page": 1,
                                "bbox": [0, 0, img_width, img_height],
                                "bytes": img_bytes,
                                "sha1": sha1,
                            }
                        ],
                        # bbox 기반 요소 추가 (VLM 결과 삽입을 위해)
                        "elements": [
                            {
                                "type": "image",
                                "image_id": "page-1-img-1",
                                "bbox": [0, 0, img_width, img_height],
                            }
                        ],
                    }
                ],
            }
        elif ext in {".xlsx", ".xls"}:
            return self._process_excel(path, language, output_base_dir, progress_callback)

        elif ext == ".csv":
            # CSV는 표 스타일 정보 없으므로 기존 방식 유지
            from core.converters import parse_csv
            pdf_result = parse_csv(path)

            content = build_document_text(
                source_path=path,
                page_count=pdf_result["page_count"],
                pages=pdf_result["pages"],
                image_results={},
            )
            meta = {
                "source": str(path),
                "page_count": pdf_result["page_count"],
                "images": [],
            }
            if output_base_dir is not None:
                output_path = self._relative_output_path(path, base_dir=output_base_dir)
            else:
                output_path = self._relative_output_path(path)
            write_outputs(output_path, content, meta)
            if progress_callback:
                progress_callback(str(output_path), pdf_result["page_count"], pdf_result["page_count"])
            return output_path

        elif ext == ".hwpx":
            return self._process_hwpx_direct(path, language, output_base_dir, progress_callback)

        elif ext == ".hwp":
            try:
                return self._process_hwp_direct(path, language, output_base_dir, progress_callback)
            except Exception as exc:  # noqa: BLE001
                print(f"[WARNING] HWP direct 파싱 실패, 기존 PDF/VLM fallback으로 전환: {exc}")

            # HWP fallback: 페이지 단위 PDF/VLM 처리
            resume_from = resume_info.get("resume_from_page") if resume_info else None
            existing_out = Path(resume_info["existing_output_path"]) if resume_info and resume_info.get("existing_output_path") else None
            return self._process_hwp_page_by_page(
                path,
                language,
                output_base_dir=output_base_dir,
                progress_callback=progress_callback,
                resume_from_page=resume_from,
                existing_output_path=existing_out,
            )
        else:
            pdf_path = self._to_pdf(path)
            pdf_result = extract_pdf(pdf_path)
            
            # PDF 페이지에 텍스트가 거의 없거나 부분 이미지만 있는 경우 전체 페이지를 이미지로 처리
            # 단, 텍스트 기반 표 이미지가 이미 있는 페이지는 중복 방지를 위해 건너뜀
            import fitz
            doc = fitz.open(pdf_path)
            for page_idx in range(len(pdf_result["pages"])):
                page_data = pdf_result["pages"][page_idx]
                page_text = page_data.get("text", "").strip()
                page_images = page_data.get("images", [])

                if page_data.get("merged_table_bboxes"):
                    print(f"[DEBUG] Page {page_idx+1}: 이전 페이지 표에 병합된 페이지 → 전체 페이지 렌더링 생략")
                    continue

                # 텍스트 기반 표 이미지가 이미 존재하면 전체 페이지 렌더링 생략 (중복 방지)
                has_text_tables = any(img.get("is_text_table", False) for img in page_images)
                if has_text_tables:
                    print(f"[DEBUG] Page {page_idx+1}: 텍스트 기반 표 이미지 존재 → 전체 페이지 렌더링 생략 (중복 방지)")
                    continue

                # 표 셀 내부 이미지가 이미 셀 설명 후보로 귀속된 경우, 이를 부분 이미지만 있는 페이지로
                # 오해해 전체 페이지 표 VLM을 추가하면 동일 표가 중복 추출된다.
                has_embedded_cell_images = any(
                    img.get("is_embedded_in_table_cell", False) for img in page_images
                )
                has_native_tables = any(
                    elem.get("type") == "native_table" for elem in page_data.get("elements", [])
                )
                if has_embedded_cell_images and has_native_tables:
                    print(f"[DEBUG] Page {page_idx+1}: 표 셀 내부 이미지 존재 → 전체 페이지 렌더링 생략 (중복 방지)")
                    continue

                # 조건 1: 텍스트가 매우 적음 (200자 미만)
                # 조건 2: 이미지가 없거나, 있어도 부분 이미지 (페이지 전체가 아님)
                needs_fullpage = False

                if len(page_text) < 200:
                    if len(page_images) == 0:
                        needs_fullpage = True
                        print(f"[DEBUG] Page {page_idx+1}: 텍스트 부족 & 이미지 없음 → 전체 페이지 변환")
                    else:
                        # 이미지가 있지만 페이지의 일부만 차지하는지 확인
                        page = doc.load_page(page_idx)
                        page_width = page.rect.width
                        page_height = page.rect.height
                        page_area = page_width * page_height
                        
                        # 모든 이미지의 총 면적 계산
                        total_img_area = 0
                        for img in page_images:
                            bbox = img.get("bbox", [0, 0, 0, 0])
                            img_area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
                            total_img_area += img_area
                        
                        # 이미지가 페이지의 70% 미만을 차지하면 부분 이미지로 간주
                        if total_img_area < page_area * 0.7:
                            needs_fullpage = True
                            coverage = (total_img_area / page_area) * 100
                            print(f"[DEBUG] Page {page_idx+1}: 부분 이미지만 존재 (커버리지 {coverage:.1f}%) → 전체 페이지 변환")
                
                if needs_fullpage:
                    # 페이지를 이미지로 렌더링
                    page = doc.load_page(page_idx)
                    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))  # 2배 확대
                    img_bytes = pix.tobytes("png")
                    
                    from PIL import Image
                    pil_img = Image.open(io.BytesIO(img_bytes))
                    img_width, img_height = pil_img.size
                    
                    sha1 = hashlib.sha1(img_bytes).hexdigest()
                    
                    # 전체 페이지 이미지를 images 리스트에 추가
                    full_page_img = {
                        "image_id": f"page-{page_idx+1}-fullpage",
                        "page": page_idx + 1,
                        "bbox": [0, 0, img_width, img_height],
                        "bytes": img_bytes,
                        "sha1": sha1,
                        "is_table": True,  # 표로 간주하여 TABLE_PROMPT 사용
                    }
                    
                    # 기존 이미지가 없으면 생성
                    if "images" not in page_data:
                        page_data["images"] = []
                    
                    # 전체 페이지 이미지를 맨 앞에 추가
                    page_data["images"].insert(0, full_page_img)
                    
                    # elements에도 추가 (bytes 포함)
                    if "elements" not in page_data:
                        page_data["elements"] = []
                    page_data["elements"].insert(0, {
                        "type": "image",
                        "image_id": f"page-{page_idx+1}-fullpage",
                        "bbox": [0, 0, img_width, img_height],
                        "bytes": img_bytes,
                        "sha1": sha1,
                    })
            
            doc.close()

        image_results: Dict[str, Dict[str, Any]] = {}
        all_images: List[Dict[str, Any]] = []
        page_count = int(pdf_result.get("page_count", len(pdf_result.get("pages", [])) or 1))
        for page in pdf_result["pages"]:
            page_num = page.get("page", 1)
            for img in page.get("images", []):
                # page 키가 없으면 추가
                if "page" not in img:
                    img["page"] = page_num
                all_images.append(img)

        # VLM 분석 수행 (이미지별 병렬 처리)
        if all_images:
            print(f"[DEBUG] 총 {len(all_images)}개 이미지 VLM 분석 시작")
            from concurrent.futures import ThreadPoolExecutor, as_completed

            def _analyze_image_pdf(image):
                """단일 이미지 VLM 분석 (병렬 실행용)"""
                _img_bytes = image.get("bytes")
                _img_id = image.get("image_id", "")
                if not (_img_bytes and _img_id):
                    return _img_id, {"text": "", "error": "missing bytes or id"}
                try:
                    _is_table = image.get("is_table", False) or image.get("is_text_table", False)
                    _is_flowchart = image.get("is_flowchart", None)
                    _res = self.vlm.describe_image(
                        _img_bytes,
                        language=language,
                        is_table=_is_table if _is_table else None,
                        is_flowchart=_is_flowchart,
                        is_math=None
                    )
                    return _img_id, _res
                except Exception as _e:
                    print(f"[WARNING] 이미지 {_img_id} VLM 분석 실패: {_e}")
                    import traceback
                    traceback.print_exc()
                    return _img_id, {"text": "", "error": str(_e)}

            _is_local_gpu = getattr(self.vlm, 'use_qwen', False)
            _concurrency = self.config.gpu_max_concurrent if _is_local_gpu else self.config.vlm_max_concurrent
            _img_workers = min(len(all_images), _concurrency)
            image_ids_by_page: Dict[int, set[str]] = {}
            completed_image_ids: set[str] = set()
            completed_pages = {
                int(page.get("page", idx + 1))
                for idx, page in enumerate(pdf_result["pages"])
                if not page.get("images", [])
            }
            if progress_callback and completed_pages:
                progress_callback(None, len(completed_pages), page_count)
            with ThreadPoolExecutor(max_workers=_img_workers) as executor:
                for img in all_images:
                    img_id = img.get("image_id", "")
                    if not img_id:
                        continue
                    page_num = int(img.get("page", 1) or 1)
                    image_ids_by_page.setdefault(page_num, set()).add(img_id)
                futures = {
                    executor.submit(_analyze_image_pdf, img): img
                    for img in all_images
                }
                for future in tqdm(as_completed(futures), total=len(all_images), desc="VLM 이미지 분석"):
                    _img_id, _res = future.result()
                    if _img_id:
                        image_results[_img_id] = _res
                        completed_image_ids.add(_img_id)
                        newly_completed = False
                        for page_num, page_image_ids in image_ids_by_page.items():
                            if page_num in completed_pages:
                                continue
                            if page_image_ids and page_image_ids.issubset(completed_image_ids):
                                completed_pages.add(page_num)
                                newly_completed = True
                        if progress_callback and newly_completed:
                            progress_callback(None, len(completed_pages), page_count)
            print(f"[DEBUG] VLM 분석 완료: {len(image_results)}개 결과")
        elif progress_callback:
            progress_callback(None, page_count, page_count)

        content = build_document_text(
            source_path=path,
            page_count=pdf_result["page_count"],
            pages=pdf_result["pages"],
            image_results=image_results,
        )

        meta = {
            "source": str(path),
            "page_count": pdf_result["page_count"],
            "images": [
                {
                    "image_id": img.get("image_id", ""),
                    "page": img.get("page", 1),
                    "bbox": img.get("bbox", [0, 0, 0, 0]),
                    "sha1": img.get("sha1", ""),
                    "vlm": image_results.get(img.get("image_id", ""), {}),
                }
                for img in all_images
            ],
        }

        # output_base_dir이 지정되면 해당 디렉토리를 기준으로 상대 경로 계산
        if output_base_dir is not None:
            output_path = self._relative_output_path(path, base_dir=output_base_dir)
        else:
            output_path = self._relative_output_path(path)
        write_outputs(output_path, content, meta)
        if progress_callback:
            progress_callback(str(output_path), page_count, page_count)
        return output_path

    def process_directory(self, root: Path, language: str = "한국어") -> List[Path]:
        """
        디렉토리를 재귀적으로 처리하고 outputs에 동일한 구조 생성
        """
        root = root.resolve()
        results: List[Path] = []
        
        # 지원되는 파일 확장자
        supported_extensions = SUPPORTED_DOC.union({".pdf"}).union(IMAGE_EXT)
        
        # 재귀적으로 모든 파일 찾기
        all_files = []
        for dirpath, _, filenames in os.walk(root):
            for filename in filenames:
                path = Path(dirpath) / filename
                if path.suffix.lower() in supported_extensions:
                    all_files.append(path)
        
        print(f"[DEBUG] 디렉토리 처리 시작: {root}")
        print(f"[DEBUG] 발견된 파일 수: {len(all_files)}개")
        
        # 각 파일 처리 (output_base_dir을 root로 설정하여 동일한 구조 유지)
        for path in tqdm(all_files, desc="파일 처리"):
            try:
                out = self.process_file(path, language=language, output_base_dir=root)
                results.append(out)
            except ConversionError as e:
                print(f"[WARN] 변환 실패 {path}: {e}")
            except Exception as e:
                print(f"[WARN] 처리 실패 {path}: {e}")
                import traceback
                traceback.print_exc()
        
        print(f"[DEBUG] 디렉토리 처리 완료: {len(results)}개 파일 처리됨")
        return results
