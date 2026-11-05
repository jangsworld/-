"""주민등록번호 형식 검증·마스킹 및 생년월일/성별 산출.

주민등록번호는 employee_rrn 테이블에 암호화 저장하며, 화면·문서·CSV 에는 생년월일만 사용합니다.
"""
import re
from datetime import date

_RRN = re.compile(r"^(\d{6})-?(\d{7})$")
_CENTURY = {"1": 1900, "2": 1900, "5": 1900, "6": 1900, "3": 2000, "4": 2000, "7": 2000, "8": 2000,
            "9": 1800, "0": 1800}


def birth_from(front: str, code: str) -> str | None:
    """앞 6자리 + 뒷자리 첫 숫자 → YYYY-MM-DD."""
    try:
        return date(_CENTURY[code] + int(front[:2]), int(front[2:4]), int(front[4:6])).isoformat()
    except (KeyError, ValueError):
        return None


def gender_from(code: str) -> str:
    return "남" if int(code) % 2 else "여"


def normalize(value: str | None) -> str | None:
    """형식이 맞으면 '######-#######' 로 반환, 아니면 None."""
    m = _RRN.match(re.sub(r"\s", "", value or ""))
    if not m or not birth_from(m.group(1), m.group(2)[0]):
        return None
    return f"{m.group(1)}-{m.group(2)}"


def parse(value: str) -> dict:
    n = normalize(value)
    return {"birth_date": birth_from(n[:6], n[7]), "gender": gender_from(n[7])} if n else {}


def mask(value: str | None) -> str:
    return f"{value[:8]}******" if value else ""
