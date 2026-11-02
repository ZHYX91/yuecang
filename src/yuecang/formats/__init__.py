"""Local document extraction. No network access or implicit model downloads."""
from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path

from .common import ExtractionError, ExtractionResult, MAX_INPUT_BYTES, resource_budget

__all__ = ["extract", "ExtractionResult", "ExtractionError"]


def extract(path: Path, *, ocr: bool = False, resource_dir: Path | None = None) -> ExtractionResult:
    root = Path(resource_dir) if resource_dir is not None else Path(tempfile.mkdtemp(prefix="yuecang-resources-"))
    with resource_budget(root):
        return _extract(path, ocr=ocr, resource_dir=root)


def _extract(path: Path, *, ocr: bool, resource_dir: Path) -> ExtractionResult:
    path = Path(path)
    if not path.is_file():
        raise ExtractionError("source_not_a_file")
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise ExtractionError("source_size_limit")
    root = resource_dir
    suffix = path.suffix.casefold()
    try:
        if suffix in {".txt", ".md", ".markdown"}:
            from .plaintext import extract_plain
            result = extract_plain(path, markdown=suffix != ".txt", resource_dir=root)
        elif suffix == ".epub":
            from .epub import extract_epub
            result = extract_epub(path, root)
        elif suffix == ".docx":
            from .docx import extract_docx
            result = extract_docx(path, root)
        elif suffix == ".pdf":
            from .pdf import extract_pdf
            result = extract_pdf(path, ocr=ocr, resource_dir=root)
        elif suffix in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}:
            from .pdf import extract_image
            result = extract_image(path, ocr=ocr, resource_dir=root)
        else:
            result = ExtractionResult(path.stem, parser="unsupported")
            result.issue("unsupported_format", f"尚不支持 {suffix or '无扩展名'} 格式", severity="error")
    except ImportError as exc:
        result = ExtractionResult(path.stem, parser="dependency-unavailable")
        result.issue("dependency_unavailable", f"缺少格式组件：{exc.name}", severity="error")
    except (ExtractionError, zipfile.BadZipFile, KeyError, UnicodeError) as exc:
        result = ExtractionResult(path.stem, parser="failed")
        result.issue("extraction_failed", "文件结构损坏、不受支持或超过处理上限", severity="error", reason=type(exc).__name__)
    result.metadata["resource_directory"] = str(root.resolve())
    return result
