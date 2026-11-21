"""증명서에 넣는 법인 도장(직인)·회사 로고 그림.

- 회사정보·환경설정 화면에서 관리자만 올립니다. PNG·JPG 만, 500KB 이하.
- 그림은 DB 설정(doc_seal / doc_logo)에 암호화해 둡니다. 코드·저장소에는 들어가지 않습니다.
- 문서 본문에는 그림을 넣지 않고 자리(class="doc-seal", "doc-logo")만 둡니다. 화면은 권한 있는 주소에서,
  PDF 는 변환할 때 그림을 끼워 넣습니다. 그래서 도장을 바꾸면 새로 만드는 PDF 부터 바뀝니다.
"""
import base64
import hashlib

from app.db import audit, batch, get_settings, set_setting
from app.security.crypto import enc

KINDS = {"seal": "법인 도장(직인)", "logo": "회사 로고"}
MAX_BYTES = 500 * 1024
_MAGIC = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"))
_BOX = {
    "seal": ".paper .doc-seal{display:inline-block;width:20mm;height:20mm;margin:-7mm 0 -7mm -4mm;vertical-align:middle;"
            "overflow:hidden;color:transparent;font-size:1px;background:url(%s) center/contain no-repeat;"
            "-webkit-print-color-adjust:exact;print-color-adjust:exact}",
    "logo": ".paper .doc-logo{display:block;width:50mm;height:13mm;background:url(%s) left center/contain no-repeat;"
            "-webkit-print-color-adjust:exact;print-color-adjust:exact}",
}


def get(kind: str) -> tuple[bytes, str] | None:
    """(그림, 종류) 또는 None."""
    if kind not in KINDS:
        return None
    mime, _, b64 = (get_settings().get(f"doc_{kind}") or "").partition(";")
    if not b64 or mime not in ("image/png", "image/jpeg"):
        return None
    try:
        return base64.b64decode(b64), mime
    except ValueError:
        return None


def save(kind: str, raw: bytes) -> str:
    """잘못된 파일이면 이유를 돌려주고 저장하지 않습니다."""
    if kind not in KINDS:
        return "알 수 없는 종류입니다"
    if not raw:
        return "파일을 고르세요"
    if len(raw) > MAX_BYTES:
        return "그림이 너무 큽니다 (500KB 이하로 줄여 주세요)"
    mime = next((m for sig, m in _MAGIC if raw.startswith(sig)), None)
    if not mime:
        return "PNG 또는 JPG 그림만 올릴 수 있습니다"
    with batch():
        set_setting(f"doc_{kind}", enc(mime + ";" + base64.b64encode(raw).decode()))
        audit("system", "설정 변경", KINDS[kind], f"그림 올림 {len(raw) // 1024}KB")
    return ""


def remove(kind: str):
    if kind in KINDS:
        with batch():
            set_setting(f"doc_{kind}", "")
            audit("system", "설정 변경", KINDS[kind], "그림 지움")


def css(embed: bool = False) -> str:
    """도장·로고 자리에 그림을 그리는 CSS. embed=True 는 PDF 용(그림을 CSS 안에 넣음), False 는 화면용(주소)."""
    out = []
    for kind in KINDS:
        got = get(kind)
        if not got:
            continue
        raw, mime = got
        if embed:
            url = f"data:{mime};base64,{base64.b64encode(raw).decode()}"
        else:
            url = f"/hr/api/doc-asset/{kind}?v={hashlib.sha256(raw).hexdigest()[:10]}"
        out.append(_BOX[kind] % url)
    return "".join(out)
