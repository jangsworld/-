"""개인정보·연봉 필드 암호화 (AES-256-GCM).

- 저장 형식: "enc:v1:" + base64(nonce 12B + 암호문 + 인증태그)
- 평문은 JSON 직렬화 후 암호화하므로 숫자(연봉)·문자열 타입이 그대로 복원됩니다.
- 마스터키: secrets_dir/bms_master.key (32바이트, base64). DB 와 다른 위치·별도 백업 필수.
  키를 분실하면 암호화된 데이터는 복구할 수 없습니다.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import settings

PREFIX = "enc:v1:"
_AAD = b"BMS-field-v1"
_aes: AESGCM | None = None
_mac_key: bytes = b""
UNREADABLE = "⚠ 읽을 수 없는 값"


class Enc(str):
    """enc() 가 만든 암호문 표시. 사용자가 넣은 글자가 암호문 행세를 하는 것을 구분하기 위함."""


def key_path() -> str:
    return os.path.join(settings.secrets_dir, "bms_master.key")


def load_or_create_key(allow_create: bool) -> None:
    """마스터키 로드. allow_create=True 는 DB 신규 생성 시에만 허용."""
    global _aes
    env_key = os.environ.get("BMS_MASTER_KEY")
    if env_key:
        raw = base64.b64decode(env_key)
    else:
        path = key_path()
        if not os.path.exists(path):
            if not allow_create:
                raise RuntimeError(
                    f"[BMS] 암호화 마스터키를 찾을 수 없습니다: {path}\n"
                    "      기존 DB 를 열려면 백업해 둔 키 파일을 이 위치에 복원하세요."
                )
            os.makedirs(settings.secrets_dir, exist_ok=True)
            raw = secrets.token_bytes(32)
            with open(path, "w", encoding="ascii") as f:
                f.write(base64.b64encode(raw).decode())
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            print(f"  🔑 새 암호화 마스터키 생성: {path}")
            print("     ※ 이 파일을 DB와 다른 안전한 곳(USB·금고 등)에 반드시 별도 백업하세요.")
        with open(path, encoding="ascii") as f:
            raw = base64.b64decode(f.read().strip())
    if len(raw) != 32:
        raise RuntimeError("[BMS] 마스터키 길이가 올바르지 않습니다 (32바이트 필요)")
    _aes = AESGCM(raw)
    global _mac_key
    _mac_key = hashlib.sha256(b"BMS-mac-v1" + raw).digest()


def mac(*parts) -> str:
    """서버 비밀로 만든 지문 (인증번호처럼 경우의 수가 적은 값을 DB 에 그대로 두지 않기 위함)."""
    if not _mac_key:
        raise RuntimeError("[BMS] 암호화 키가 로드되지 않았습니다")
    return hmac.new(_mac_key, ":".join(str(p) for p in parts).encode(), hashlib.sha256).hexdigest()


def _cipher() -> AESGCM:
    if _aes is None:
        raise RuntimeError("[BMS] 암호화 키가 로드되지 않았습니다")
    return _aes


def enc(value):
    """None/빈 문자열은 그대로 두고, 그 외 값은 암호화.
    이미 enc() 가 만든 값(Enc)만 그대로 통과. 사용자가 'enc:v1:' 로 시작하는 글자를 넣으면 일반 글자로 보고 암호화."""
    if value is None or value == "":
        return value
    if isinstance(value, Enc):
        return value
    nonce = secrets.token_bytes(12)
    ct = _cipher().encrypt(nonce, json.dumps(value, ensure_ascii=False).encode(), _AAD)
    return Enc(PREFIX + base64.b64encode(nonce + ct).decode())


def dec(value):
    """풀지 못하는 값(깨진 값·남이 넣은 흉내 글자)은 그 칸만 '읽을 수 없는 값'으로 — 화면 전체가 멈추지 않게."""
    if not isinstance(value, str) or not value.startswith(PREFIX):
        return value
    try:
        blob = base64.b64decode(value[len(PREFIX):], validate=True)
        pt = _cipher().decrypt(blob[:12], blob[12:], _AAD)
        return json.loads(pt)
    except Exception:                                   # noqa: BLE001  (base64·인증태그·JSON 어느 단계든)
        logging.getLogger("bms.crypto").warning("복호화할 수 없는 값을 만남 (길이 %d)", len(value))
        return UNREADABLE


def check_params(params) -> None:
    """DB 에 넣는 값 검사: enc() 를 거치지 않은 글자가 암호문 머리글자로 시작하면 거부.
    (남의 암호문을 일반 칸에 붙여 넣어 화면이 풀어 보여 주게 하거나, 화면을 멈추게 하는 것을 막음)"""
    for p in (params.values() if isinstance(params, dict) else params):
        if isinstance(p, str) and not isinstance(p, Enc) and p.startswith(PREFIX):
            raise ValueError("허용되지 않는 값입니다")


def dec_row(row: dict) -> dict:
    return {k: dec(v) for k, v in row.items()}
