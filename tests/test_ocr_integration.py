"""Optional real OCR integration, using only generated pages."""
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from yuecang.application.service import Workbench


def test_real_tesseract_image_and_scanned_pdf(tmp_path, monkeypatch):
    executable = shutil.which("tesseract")
    if executable is None:
        pytest.skip("Optional Tesseract is not installed")
    languages = subprocess.run([executable, "--list-langs"], capture_output=True, text=True, check=True).stdout
    if "eng" not in languages.split():
        pytest.skip("Optional English OCR language is not installed")
    font_path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    font = ImageFont.truetype(str(font_path), 40) if font_path.exists() else ImageFont.load_default(size=40)
    picture = Image.new("RGB", (900, 220), "white")
    ImageDraw.Draw(picture).text((30, 60), "WATER 250 mL EVERY WEEK", fill="black", font=font)
    image_path, pdf_path = tmp_path / "synthetic.png", tmp_path / "synthetic.pdf"
    picture.save(image_path)
    picture.save(pdf_path, "PDF", resolution=150)
    monkeypatch.setenv("YUECANG_OCR_LANG", "eng")
    wb = Workbench(tmp_path / "project")
    for path in (image_path, pdf_path):
        source = wb.source(wb.import_file(path, ocr=True)["source_id"])
        text = " ".join(block["text"] for block in source["blocks"])
        assert "WATER" in text and "250" in text
        assert any(block.get("attributes", {}).get("ocr") for block in source["blocks"])
        assert any(block["resources"] for block in source["blocks"])
        assert any(issue["code"] == "ocr_review_required" for issue in source["issues"])
