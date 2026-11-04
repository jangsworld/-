"""개인정보·연봉 필드 암호화 (AES-256-GCM).

- 저장 형식: "enc:v1:" + base64(nonce 12B + 암호문 + 인증태그)
- 평문은 JSON 직렬화 후 암호화하므로 숫자(연봉)·문자열 타입이 그대로 복원됩니다.
- 마스터키: secrets_dir/bms_master.key (32바이트, base64). DB 와 다른 위치·별도 백업 필수.
  키를 분실하면 암호화된 데이터는 복구할 수 없습니다.
"""
import base64
import json
import os
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import settings

PREFIX = "enc:v1:"
_AAD = b"BMS-field-v1"
_aes: AESGCM | None = None


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


def _cipher() -> AESGCM:
    if _aes is None:
        raise RuntimeError("[BMS] 암호화 키가 로드되지 않았습니다")
    return _aes


def enc(value):
    """None/빈 문자열은 그대로 두고, 그 외 값은 암호화."""
    if value is None or value == "":
        return value
    if isinstance(value, str) and value.startswith(PREFIX):
        return value
    nonce = secrets.token_bytes(12)
    ct = _cipher().encrypt(nonce, json.dumps(value, ensure_ascii=False).encode(), _AAD)
    return PREFIX + base64.b64encode(nonce + ct).decode()


def dec(value):
    if not isinstance(value, str) or not value.startswith(PREFIX):
        return value
    blob = base64.b64decode(value[len(PREFIX):])
    pt = _cipher().decrypt(blob[:12], blob[12:], _AAD)
    return json.loads(pt)


def dec_row(row: dict) -> dict:
    return {k: dec(v) for k, v in row.items()}
