"""증명서에 넣는 법인 도장(직인)·회사 로고 그림.

- 회사정보·환경설정 화면에서 관리자만 올립니다. PNG·JPG·WEBP·BMP·GIF, 4MB 이하.
  올린 그림은 서버에서 다시 그려(크기를 줄이고 PNG 로) 저장하므로 그림 파일에 다른 것이 섞여 있어도 남지 않습니다.
  도장은 흰 종이 바탕을 자동으로 투명하게 만듭니다(이름 글자를 가리지 않게).
- 그림은 DB 설정(doc_seal / doc_logo)에 암호화해 둡니다. 코드·저장소에는 들어가지 않습니다.
- 문서 본문에는 그림을 넣지 않고 자리(class="doc-seal", "doc-logo")만 둡니다. 화면은 권한 있는 주소에서,
  PDF 는 변환할 때 그림을 끼워 넣습니다. 그래서 도장을 바꾸면 새로 만드는 PDF 부터 바뀝니다.
"""
import base64
import hashlib
import io

from PIL import Image, ImageOps, UnidentifiedImageError

from app import company
from app.db import audit, batch, get_settings, set_setting
from app.security.crypto import enc

KINDS = {"seal": "법인 도장(직인)", "logo": "회사 로고"}
MAX_BYTES = 4 * 1024 * 1024
FORMATS = {"PNG", "JPEG", "WEBP", "BMP", "GIF"}
SIDE = {"seal": 500, "logo": 1000}          # 저장할 때 긴 쪽 최대 픽셀
_BOX = {
    "seal": ".paper .doc-seal{display:inline-block;width:20mm;height:20mm;margin:-7mm 0 -7mm -4mm;vertical-align:middle;"
            "overflow:hidden;color:transparent;font-size:1px;background:url(%s) center/contain no-repeat;"
            "-webkit-print-color-adjust:exact;print-color-adjust:exact}",
    "logo": ".paper .doc-logo{display:block;width:50mm;height:13mm;background:url(%s) left center/contain no-repeat;"
            "-webkit-print-color-adjust:exact;print-color-adjust:exact}",
}


def get(kind: str, co: str = "1") -> tuple[bytes, str] | None:
    """(그림, 종류) 또는 None. co: 회사 ('1' 본사 / '2' 자회사)."""
    if kind not in KINDS:
        return None
    mime, _, b64 = (get_settings().get(company.key(f"doc_{kind}", company.norm(co))) or "").partition(";")
    if not b64 or mime not in ("image/png", "image/jpeg"):
        return None
    try:
        return base64.b64decode(b64), mime
    except ValueError:
        return None


def _clear_paper(img: Image.Image) -> Image.Image:
    """도장: 이미 투명한 곳이 있으면 그대로, 없으면 흰 종이 바탕(밝은 색)을 투명하게."""
    if img.getchannel("A").getextrema()[0] < 250:
        return img
    gray = img.convert("RGB").convert("L")
    img.putalpha(gray.point(lambda v: 0 if v >= 225 else (255 if v <= 170 else int((225 - v) * 255 / 55))))
    return img


def save(kind: str, raw: bytes, co: str = "1") -> str:
    """잘못된 파일이면 이유를 돌려주고 저장하지 않습니다."""
    if kind not in KINDS:
        return "알 수 없는 종류입니다"
    if not raw:
        return "파일을 고르세요"
    if len(raw) > MAX_BYTES:
        return "그림이 너무 큽니다 (4MB 이하 그림을 올려 주세요)"
    try:
        with Image.open(io.BytesIO(raw)) as src:
            if src.format not in FORMATS:
                return "그림 파일(PNG·JPG 등)만 올릴 수 있습니다"
            img = ImageOps.exif_transpose(src).convert("RGBA")
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        return "그림을 읽을 수 없습니다. PNG 또는 JPG 그림을 올려 주세요"
    if kind == "seal":
        img = _clear_paper(img)
    box = img.getchannel("A").getbbox()             # 둘레의 빈 곳을 잘라 그림이 자리를 꽉 채우게
    if box:
        img = img.crop(box)
    img.thumbnail((SIDE[kind], SIDE[kind]), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    out = buf.getvalue()
    with batch():
        set_setting(company.key(f"doc_{kind}", company.norm(co)), enc("image/png;" + base64.b64encode(out).decode()))
        audit("system", "설정 변경", KINDS[kind], f"그림 올림 {len(out) // 1024}KB" + (" (자회사)" if company.norm(co) == "2" else ""))
    return ""


def remove(kind: str, co: str = "1"):
    if kind in KINDS:
        with batch():
            set_setting(company.key(f"doc_{kind}", company.norm(co)), "")
            audit("system", "설정 변경", KINDS[kind], "그림 지움")


def css(embed: bool = False, co: str = "1") -> str:
    """도장·로고 자리에 그림을 그리는 CSS. embed=True 는 PDF 용(그림을 CSS 안에 넣음), False 는 화면용(주소)."""
    out = []
    for kind in KINDS:
        co = company.norm(co)
        got = get(kind, co)
        if not got:
            continue
        raw, mime = got
        if embed:
            url = f"data:{mime};base64,{base64.b64encode(raw).decode()}"
        else:
            url = f"/hr/api/doc-asset/{kind}?" + ("c=2&" if co == "2" else "") + f"v={hashlib.sha256(raw).hexdigest()[:10]}"
        out.append(_BOX[kind] % url)
    return "".join(out)


def mail_logo(co: str = "1") -> tuple[bytes, str] | None:
    """메일 머리에 넣는 로고: 둘레의 흰 여백·투명한 곳을 잘라 내고 작게(너비 360픽셀 이하) 줄인 PNG. 없으면 None."""
    got = get("logo", co)
    if not got:
        return None
    try:
        with Image.open(io.BytesIO(got[0])) as src:
            img = src.convert("RGBA")
        flat = Image.new("RGB", img.size, (255, 255, 255))
        flat.paste(img, mask=img.getchannel("A"))
        box = flat.convert("L").point(lambda v: 255 if v < 245 else 0).getbbox()
        if box:
            flat = flat.crop(box)
        flat.thumbnail((360, 200), Image.LANCZOS)        # 메일에서는 너비 180 으로 보임(선명하게 2배)
        buf = io.BytesIO()
        flat.save(buf, "PNG", optimize=True)
        return buf.getvalue(), "image/png"
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        return None
