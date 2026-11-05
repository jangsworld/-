"""서버 점검: 시간대, PDF 변환, 한글 OCR 이 서비스 환경에서 동작하는지 확인합니다."""
import glob
import os
import sys
import time

sys.path.insert(0, os.getcwd())

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from app.modules.hr import intake  # noqa: E402
from app.storage import render_pdf  # noqa: E402


def check(name, fn):
    try:
        print(f"[정상] {name}: {fn()}")
        return True
    except Exception as ex:
        print(f"[실패] {name}: {ex.__class__.__name__}: {ex}")
        return False


def pdf():
    data = render_pdf("<h1>근로계약서</h1><p>한글 PDF 변환 점검</p>", "점검")
    if not data.startswith(b"%PDF"):
        raise ValueError("PDF 형식이 아닙니다")
    return f"{len(data):,} bytes"


def ocr():
    fonts = glob.glob("/usr/share/fonts/**/NotoSansCJK-Regular.ttc", recursive=True)
    if not fonts:
        raise FileNotFoundError("Noto CJK 글꼴")
    img = Image.new("RGB", (1000, 160), "white")
    ImageDraw.Draw(img).text((30, 40), "주민등록등본 근로계약서", fill="black", font=ImageFont.truetype(fonts[0], 60))
    text = intake._ocr_image(img) or ""
    if "근로계약서" not in text.replace(" ", ""):
        raise ValueError(f"인식 결과: {text!r}")
    return text.strip()


results = [
    check("Python", lambda: sys.version.split()[0]),
    check("시간대", lambda: time.strftime("%Y-%m-%d %H:%M %Z")),
    check("PDF 변환", pdf),
    check("한글 OCR", ocr),
]
sys.exit(0 if all(results) else 1)
