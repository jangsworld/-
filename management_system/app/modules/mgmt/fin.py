"""연도별 손익 요약: fund에서 읽기 전용으로 가져오거나 직접 입력.

fund는 BMS 서버에서, 열쇠를 가진 요청에만, 요청한 한 해의 손익 요약(매출액~당기순손익)만 내줍니다.
fund 인증서는 자체 서명이므로 설정에 넣은 SHA-256 지문과 맞을 때만 자료를 받습니다.
"""
import hashlib
import http.client
import json
import ssl
from urllib.parse import urlparse

from app.db import audit, execute, get_settings, query_one
from app.modules.mgmt.constants import FIN_ITEMS
from app.security.crypto import dec, enc

DEFAULT_FUND_URL = ""   # 회사정보·환경설정에서 입력 (공개 저장소에 내부 주소를 넣지 않음)


class FundError(Exception):
    pass


def _norm_fp(v: str) -> str:
    return "".join(ch for ch in (v or "").upper() if ch in "0123456789ABCDEF")


def fund_configured() -> bool:
    s = get_settings()
    return bool(s.get("fund_token") and s.get("fund_cert_sha256"))


def fetch_fund(year: int, timeout: int = 15) -> dict:
    """fund에서 그 해 손익 요약을 읽어 반환 (저장은 하지 않음)."""
    s = get_settings()
    token = dec(s.get("fund_token") or "")
    pin = _norm_fp(s.get("fund_cert_sha256", ""))
    if not token or len(pin) != 64:
        raise FundError("fund 연결 설정(열쇠·인증서 지문)이 없습니다. 회사정보·환경설정 → fund 연결에서 넣어 주세요.")
    u = urlparse(s.get("fund_url") or DEFAULT_FUND_URL)
    if not u.scheme:
        raise FundError("fund 주소가 없습니다. 회사정보·환경설정 → fund 연결에서 넣어 주세요.")
    if u.scheme != "https" or not u.hostname:
        raise FundError("fund 주소는 https:// 로 시작해야 합니다")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE          # 자체 서명 인증서 → 아래에서 지문으로 직접 확인
    conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout, context=ctx)
    try:
        conn.connect()
        der = conn.sock.getpeercert(binary_form=True)
        if hashlib.sha256(der).hexdigest().upper() != pin:
            raise FundError("fund 인증서 지문이 설정과 다릅니다. 연결하지 않았습니다.")
        conn.request("GET", f"/api/bms/annual?year={int(year)}", headers={"Authorization": f"Bearer {token}"})
        r = conn.getresponse()
        body = r.read(200_000)
    except FundError:
        raise
    except (OSError, ssl.SSLError, http.client.HTTPException) as ex:
        raise FundError(f"fund에 연결하지 못했습니다 ({type(ex).__name__})") from ex
    finally:
        conn.close()
    try:
        d = json.loads(body or b"{}")
    except ValueError as ex:
        raise FundError("fund 응답을 읽지 못했습니다") from ex
    if r.status == 403:
        raise FundError("fund가 거절했습니다. 열쇠가 바뀌었거나 끊겼는지 fund 설정에서 확인하세요.")
    if r.status == 404:
        raise FundError(d.get("message") or f"{year}년 자료가 fund에 없습니다")
    if r.status != 200 or not d.get("ok") or d.get("year") != int(year):
        raise FundError(f"fund 응답 오류 ({r.status})")
    items = {k: {kk: (int(vv) if isinstance(vv, (int, float)) else None) for kk, vv in (v or {}).items()
                 if kk in ("실적", "연말예상", "확정")}
             for k, v in (d.get("items") or {}).items() if k in FIN_ITEMS}
    return {"basis": d.get("basis") or "", "period": d.get("period") or "", "as_of": d.get("as_of") or "", "items": items}


def save(year: int, source: str, data: dict, by: str = ""):
    execute("INSERT INTO mgmt_fin_year(year, source, basis, data, fetched_at, fetched_by) "
            "VALUES (?,?,?,?,datetime('now','localtime'),?) ON CONFLICT(year) DO UPDATE SET source=excluded.source, "
            "basis=excluded.basis, data=excluded.data, fetched_at=excluded.fetched_at, fetched_by=excluded.fetched_by",
            (int(year), source, data.get("basis") or "", enc(data), by))


def pull(year: int, by: str = "") -> dict:
    data = fetch_fund(year)
    save(year, "fund", data, by)
    audit("mgmt", "fund 손익 요약 읽기", str(year), data.get("basis", ""))
    return data


def save_manual(year: int, values: dict, by: str = ""):
    items = {k: {"확정": int(v)} for k, v in values.items() if k in FIN_ITEMS and v is not None}
    save(year, "직접입력", {"basis": "확정", "period": "직접 입력", "as_of": "", "items": items}, by)
    audit("mgmt", "손익 요약 직접 입력", str(year), "")


def get(year: int) -> dict | None:
    r = query_one("SELECT * FROM mgmt_fin_year WHERE year=?", (int(year),))
    if not r:
        return None
    d = r["data"] if isinstance(r["data"], dict) else {}
    d = dict(d, year=r["year"], source=r["source"], fetched_at=r["fetched_at"], fetched_by=r.get("fetched_by"))
    return d


def value(d: dict | None, item: str) -> int | None:
    """확정 > 연말예상 > 실적 순으로 그 해 대표 값."""
    if not d:
        return None
    v = (d.get("items") or {}).get(item) or {}
    for k in ("확정", "연말예상", "실적"):
        if v.get(k) is not None:
            return v[k]
    return None


def summary(year: int) -> dict:
    """화면용: 그 해와 전년 값, 증감률."""
    cur, prev = get(year), get(year - 1)
    rows = []
    for item in FIN_ITEMS:
        a, b = value(cur, item), value(prev, item)
        rows.append({"item": item, "cur": a, "prev": b,
                     "rate": ((a - b) / abs(b) * 100) if (a is not None and b) else None,
                     "actual": ((cur or {}).get("items") or {}).get(item, {}).get("실적")})
    return {"year": year, "cur": cur, "prev": prev, "rows": rows, "by": {r["item"]: r for r in rows}}
