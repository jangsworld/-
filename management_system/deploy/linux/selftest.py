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
    # A4 150dpi 서류 형태와 글자가 적은 한 줄 이미지를 모두 확인
    font = ImageFont.truetype(fonts[0], 30)
    page = Image.new("RGB", (1240, 1754), "white")
    draw = ImageDraw.Draw(page)
    for i, line in enumerate(["근로계약서", "성명: 홍길동", "생년월일: 1990년 1월 1일", "근무장소: 본사", "계약기간: 2027년 1월 1일부터",
                              "업무내용: 경영지원", "위와 같이 근로계약을 체결한다."]):
        draw.text((120, 160 + i * 90), line, fill="black", font=font)
    line = Image.new("RGB", (1000, 160), "white")
    ImageDraw.Draw(line).text((30, 40), "주민등록등본 근로계약서", fill="black", font=ImageFont.truetype(fonts[0], 60))
    texts = [(intake._ocr_image(img) or "").replace(" ", "") for img in (page, line)]
    if "성명:홍길동" not in texts[0].replace("\n", "") or "근로계약서" not in texts[1]:
        raise ValueError(f"인식 결과: {texts!r}")
    return "A4 서류·한 줄 이미지 모두 인식"


results = [
    check("Python", lambda: sys.version.split()[0]),
    check("시간대", lambda: time.strftime("%Y-%m-%d %H:%M %Z")),
    check("PDF 변환", pdf),
    check("한글 OCR", ocr),
]
sys.exit(0 if all(results) else 1)
