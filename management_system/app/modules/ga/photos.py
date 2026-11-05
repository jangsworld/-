"""자산 사진 저장: 저장장치 부담을 줄이도록 작은 WebP 로 변환합니다.

휴대폰 사진(수 MB)도 본 사진 약 50~100KB, 목록용 썸네일 약 3KB 로 줄어듭니다.
파일명은 자산 id 로만 만들어 업로드 파일명이 경로에 쓰이지 않습니다.
"""
import io
import os

from PIL import Image, ImageOps, UnidentifiedImageError

from app.config import settings

MAX_SIDE = 1024
THUMB_SIDE = 96
QUALITY = 60
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP", "BMP", "GIF", "MPO"}


class PhotoError(Exception):
    pass


def _folder() -> str:
    return os.path.join(os.path.dirname(settings.db_path), "asset_photos")


def path(asset_id: int, thumb: bool = False) -> str:
    return os.path.join(_folder(), f"{int(asset_id)}{'_t' if thumb else ''}.webp")


def _encode(img: Image.Image, side: int) -> bytes:
    copy = img.copy()
    copy.thumbnail((side, side), Image.LANCZOS)
    buf = io.BytesIO()
    copy.save(buf, "WEBP", quality=QUALITY, method=6)   # EXIF(촬영 위치 등)는 저장하지 않음
    return buf.getvalue()


def save(asset_id: int, raw: bytes) -> int:
    """사진을 변환해 저장하고 저장 크기(바이트)를 반환합니다."""
    try:
        with Image.open(io.BytesIO(raw)) as src:
            if src.format not in ALLOWED_FORMATS:
                raise PhotoError("JPG·PNG·WEBP 사진만 등록할 수 있습니다")
            img = ImageOps.exif_transpose(src).convert("RGB")   # 세로 사진 방향 보정
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as ex:
        raise PhotoError("사진을 읽을 수 없습니다 (아이폰 HEIC 는 JPG 로 바꿔 올려주세요)") from ex
    main, thumb = _encode(img, MAX_SIDE), _encode(img, THUMB_SIDE)
    os.makedirs(_folder(), exist_ok=True)
    with open(path(asset_id), "wb") as f:
        f.write(main)
    with open(path(asset_id, True), "wb") as f:
        f.write(thumb)
    return len(main)


def delete(asset_id: int):
    for thumb in (False, True):
        try:
            os.remove(path(asset_id, thumb))
        except FileNotFoundError:
            pass
