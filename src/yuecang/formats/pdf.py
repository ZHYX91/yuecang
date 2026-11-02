from __future__ import annotations

import csv
import math
import os
import shutil
import subprocess
import threading
from pathlib import Path

from .common import (ExtractionError, ExtractionResult, MAX_INPUT_BYTES, MAX_MEMBER_BYTES,
                     ResourceBudgetExceeded, resource_bytes, temporary_workspace)

MAX_PAGES = 2000
MAX_RENDER_PIXELS = 40_000_000
MAX_OCR_BYTES = 16 * 1024 * 1024


def _png_limit(pixels):
    # One page at a time; incompressible PNGs stop at 64 MiB. The RGB(A)
    # raster itself is bounded separately by MAX_RENDER_PIXELS.
    return min(MAX_MEMBER_BYTES, pixels * 4 + 1024 * 1024)


def _write_png(image, path, maximum):
    class BoundedPNG:
        # No fileno: Pillow must use this checked write method.
        def write(self, data):
            if stream.tell() + len(data) > maximum:
                raise ResourceBudgetExceeded("page_render_bytes_limit")
            return stream.write(data)

        def flush(self):
            stream.flush()

    with path.open("xb") as stream:
        image.save(BoundedPNG(), format="PNG")


def _ocr_to_file(executable, path, output, languages):
    """Bound stdout before disk writes, with a timeout and no captured stderr."""
    args = [executable, str(path), "stdout", "-l", languages, "tsv"]
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    failures = []

    def consume():
        try:
            size = 0
            with output.open("xb") as stream:
                while part := process.stdout.read(64 * 1024):
                    size += len(part)
                    if size > MAX_OCR_BYTES:
                        raise ResourceBudgetExceeded("ocr_output_bytes_limit")
                    stream.write(part)
        except BaseException as exc:
            failures.append(exc)
            if process.poll() is None:
                process.kill()

    reader = threading.Thread(target=consume, daemon=True)
    reader.start()
    try:
        reader.join(180)
        if reader.is_alive():
            raise subprocess.TimeoutExpired(args, 180)
        code = process.wait(timeout=5)
        if failures:
            if isinstance(failures[0], OSError):
                raise ResourceBudgetExceeded("ocr_output_write_failed") from failures[0]
            raise failures[0]
        return code
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        reader.join()
        process.stdout.close()


def _ocr_image(path: Path, result: ExtractionResult, locator: dict, resource_dir: Path | None) -> None:
    executable = shutil.which("tesseract")
    if executable is None:
        result.issue("ocr_unavailable", "未安装 Tesseract；原图已保留，可安装 OCR 组件后重新提取", severity="error", **locator)
        return
    requested = os.environ.get("YUECANG_OCR_LANG", "chi_sim+eng")
    try:
        available = subprocess.run([executable, "--list-langs"], capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        result.issue("ocr_unavailable", "OCR 组件无法启动或语言检查超时", severity="error", **locator)
        return
    languages = set(available.stdout.decode("utf-8", errors="replace").splitlines()[1:])
    selected = [x for x in requested.split("+") if x in languages]
    if not selected:
        result.issue("ocr_language_unavailable", "请求的 OCR 语言包不可用", severity="error", requested=requested, **locator)
        return
    if set(selected) != set(requested.split("+")):
        result.issue("ocr_language_partial", "部分 OCR 语言包缺失，识别结果需要核对", requested=requested, available=selected, **locator)
    with temporary_workspace(resource_dir, maximum=MAX_OCR_BYTES, prefix="ocr-") as directory:
        output = directory / "result.tsv"
        try:
            returncode = _ocr_to_file(executable, path, output, "+".join(selected))
        except (OSError, subprocess.TimeoutExpired):
            result.issue("ocr_timeout_or_unavailable", "OCR 无法启动或超过时间上限，原图保留", severity="error", **locator)
            return
        if returncode or not output.is_file():
            result.issue("ocr_failed", "OCR 进程失败，原图保留", severity="error", **locator)
            return
        groups: dict[tuple, list] = {}
        with output.open(encoding="utf-8", newline="") as stream:
            rows = csv.DictReader(stream, delimiter="\t", quoting=csv.QUOTE_NONE)
            for row in rows:
                if row.get("level") != "5" or not row.get("text"):
                    continue
                key = (row["block_num"], row["par_num"], row["line_num"])
                groups.setdefault(key, []).append(row)
        for key, words in groups.items():
            boxes = [[int(w[x]) for x in ("left", "top", "width", "height")] for w in words]
            result.add("paragraph", " ".join(w["text"] for w in words),
                       {**locator, "ocr_line": list(key), "word_boxes": boxes, "coordinate_space": "rendered_image_pixels"},
                       attributes={"ocr": True, "confidence": [float(w["conf"]) for w in words], "language": "+".join(selected)})
        if not groups:
            result.issue("ocr_no_text", "OCR 未识别出文字，不能视为已提取正文", severity="error", **locator)
        result.issue("ocr_review_required", "OCR 是派生识别结果；请对照原图校核数字、公式和专名", **locator)


def extract_image(path: Path, *, ocr: bool, resource_dir: Path | None) -> ExtractionResult:
    from PIL import Image
    result = ExtractionResult(path.stem, parser="image-tesseract-v1", metadata={"source_format": "image"})
    original = resource_bytes(path.read_bytes(), path.name, resource_dir, max_bytes=MAX_INPUT_BYTES)
    with Image.open(path) as image:
        if image.width * image.height > MAX_RENDER_PIXELS:
            raise ExtractionError("image_pixel_limit")
        result.add("figure", "", {"image": 0, "width": image.width, "height": image.height}, resources=[original])
        if getattr(image, "n_frames", 1) > 1:
            result.issue("multiframe_image", "多帧图像原件完整保留，本次仅识别首帧")
        if ocr:
            maximum = _png_limit(image.width * image.height)
            with temporary_workspace(resource_dir, maximum=maximum, prefix="image-") as directory:
                converted = directory / "page.png"
                with image.convert("RGB") as rgb:
                    _write_png(rgb, converted, maximum)
                _ocr_image(converted, result, {"image": 0, "width": image.width, "height": image.height}, resource_dir)
        else:
            result.issue("ocr_required", "图像没有文本层；启用 OCR 后可生成可编辑文本")
    return result


def extract_pdf(path: Path, *, ocr: bool, resource_dir: Path | None) -> ExtractionResult:
    from pypdf import PdfReader
    result = ExtractionResult(path.stem, parser="pypdf-tesseract-v1", metadata={"source_format": "pdf"})
    reader = PdfReader(str(path), strict=False)
    if reader.is_encrypted:
        result.issue("encrypted_pdf_unsupported", "受保护 PDF 不在当前提取范围内", severity="error")
        return result
    if len(reader.pages) > MAX_PAGES:
        raise ExtractionError("pdf_page_limit")
    if reader.metadata:
        result.title = str(reader.metadata.title or path.stem)
        result.metadata["author"] = str(reader.metadata.author or "")
    result.metadata["pages"] = len(reader.pages)
    original = resource_bytes(path.read_bytes(), path.name, resource_dir, mime="application/pdf", max_bytes=MAX_INPUT_BYTES)
    result.add("unknown", "", {"role": "original_pdf"}, resources=[original], attributes={"role": "attachment_inventory"})
    result.issue("pdf_layout_requires_review", "PDF 按页提取；多栏顺序、表格、公式和脚注须对照原页核对，原 PDF 已保留")
    for page_index, page in enumerate(reader.pages):
        locator = {"page": page_index + 1, "width": float(page.mediabox.width), "height": float(page.mediabox.height),
                   "rotation": page.rotation, "coordinate_space": "pdf_points"}
        try:
            text = page.extract_text() or ""
        except Exception:
            result.issue("pdf_text_extraction_failed", "此页文本层无法可靠提取", severity="error", **locator)
            text = ""
        if text:
            result.add("paragraph", text, locator, attributes={"page_text": True, "layout_unverified": True})
        try:
            images = list(page.images)
        except Exception:
            result.issue("pdf_images_preserved_in_original", "页内图片无法逐一提取，仍保留在原 PDF 中", **locator)
            images = []
        for image_index, image in enumerate(images):
            # Write/budget errors must propagate instead of becoming warnings.
            resource = resource_bytes(image.data, image.name, resource_dir)
            result.add("figure", "", {**locator, "image_index": image_index}, resources=[resource])
        if not text.strip():
            if not ocr:
                result.issue("ocr_required", "此页没有可读文本层；可启用 OCR", **locator)
                continue
            try:
                import pypdfium2
            except ImportError:
                result.issue("pdf_ocr_renderer_unavailable", "扫描 PDF OCR 需要 pypdfium2 页面渲染组件", severity="error", **locator)
                continue
            pixels = math.ceil(locator["width"] * 2) * math.ceil(locator["height"] * 2)
            if pixels <= 0 or pixels > MAX_RENDER_PIXELS:
                result.issue("pdf_render_pixel_limit", "此页超过 OCR 渲染像素上限", severity="error", **locator)
                continue
            maximum = _png_limit(pixels)
            with temporary_workspace(resource_dir, maximum=maximum, prefix="page-") as directory:
                document = pypdfium2.PdfDocument(str(path))
                try:
                    pdfpage = document[page_index]
                    try:
                        bitmap = pdfpage.render(scale=2)
                        try:
                            with bitmap.to_pil() as image:
                                if image.width * image.height > MAX_RENDER_PIXELS:
                                    raise ResourceBudgetExceeded("page_render_pixel_limit")
                                rendered = directory / "page.png"
                                _write_png(image, rendered, maximum)
                        finally:
                            bitmap.close()
                    finally:
                        pdfpage.close()
                finally:
                    document.close()
                resource = resource_bytes(rendered.read_bytes(), f"page-{page_index + 1}.png", resource_dir)
                result.add("figure", "", {**locator, "render_scale": 2}, resources=[resource])
                _ocr_image(rendered, result, {"page": page_index + 1, "render_scale": 2}, resource_dir)
    return result
