"""두 회사(본사·자회사) 구분.

- 회사는 '1'(본사, 기존 회사정보)과 '2'(자회사) 둘입니다. 자회사 이름(co2_company_name)을 설정에 넣어야 켜집니다.
  켜지 않으면 화면·자료가 예전과 똑같이 한 회사로 움직입니다.
- 직원은 employees.company 로 회사를 정하고, 직원에 딸린 자료(계약·연봉·검진 등)는 직원을 따라갑니다.
  총무 자료(자산·차량·문서·계약 등)는 각 표의 company 칸으로 정합니다. 비어 있으면 본사('1')로 봅니다.
- 화면 위의 회사 탭(통합 / 본사 / 자회사)에서 고른 범위는 쿠키(bms_co)에 기억하고 요청마다 scope 에 넣습니다.
  목록·현황을 만드는 질의는 sql() 조각을 붙여 그 범위만 봅니다. 한 건을 번호로 여는 질의에는 붙이지 않습니다.
- 회사 이름은 코드에 적지 않습니다 (설정값).
"""
import contextvars
import re

from app.db import get_settings, query, query_one

IDS = ("1", "2")
COOKIE = "bms_co"
scope: contextvars.ContextVar[str] = contextvars.ContextVar("bms_co", default="all")

# 회사마다 따로 두는 회사정보 (자회사는 앞에 co2_ 를 붙여 저장)
INFO_KEYS = ("company_name", "ceo_name", "company_address", "business_no", "company_phone")


def key(name: str, cid: str = "1") -> str:
    """회사별 설정 이름. 본사는 예전 이름 그대로."""
    return name if str(cid) != "2" else "co2_" + name


def norm(v) -> str:
    """자료에 적힌 회사 값 → '1' 또는 '2'."""
    return "2" if str(v or "").strip() == "2" else "1"


def enabled(s: dict | None = None) -> bool:
    s = get_settings() if s is None else s
    return bool((s.get("co2_company_name") or "").strip())


def name(cid: str = "1", s: dict | None = None) -> str:
    s = get_settings() if s is None else s
    return (s.get(key("company_name", cid)) or "").strip()


def short(cid: str = "1", s: dict | None = None) -> str:
    """탭·배지에 쓰는 짧은 이름: 따로 정한 약칭, 없으면 회사명에서 (주)·주식회사를 뺀 것."""
    s = get_settings() if s is None else s
    v = (s.get(key("company_short", cid)) or "").strip()
    if v:
        return v
    v = re.sub(r"\(주\)|㈜|주식회사", "", name(cid, s)).strip()
    return v or ("본사" if str(cid) == "1" else "자회사")


def companies(s: dict | None = None) -> list[dict]:
    s = get_settings() if s is None else s
    ids = IDS if enabled(s) else ("1",)
    return [{"id": c, "name": name(c, s), "short": short(c, s)} for c in ids]


def info(cid: str = "1", s: dict | None = None) -> dict:
    """문서에 넣는 회사정보(회사명·대표자·주소·사업자번호·전화). 자회사 값이 비어 있으면 빈 칸."""
    s = get_settings() if s is None else s
    return {k: (s.get(key(k, cid)) or "") for k in INFO_KEYS}


def cur() -> str:
    """지금 화면의 범위: 'all' / '1' / '2'. 자회사를 켜지 않았으면 언제나 'all'."""
    v = scope.get()
    return v if v in IDS and enabled() else "all"


def pick(value, fallback: str | None = None) -> str:
    """새 자료를 저장할 때의 회사: 폼 값 → 지금 범위 → 본사."""
    v = str(value or "").strip()
    if v in IDS:
        return v if (v == "1" or enabled()) else "1"
    c = fallback or cur()
    return c if c in IDS else "1"


def sql(alias: str = "e", co: str | None = None, col: str = "company") -> str:
    """목록 질의 뒤에 붙이는 조건 (' AND ...'). 값은 정해진 글자뿐이라 그대로 넣습니다."""
    c = cur() if co is None else (co if co in IDS else "all")
    if c == "all":
        return ""
    f = f"{alias}.{col}" if alias else col
    return f" AND {f}='2'" if c == "2" else f" AND ({f} IS NULL OR {f}<>'2')"


def emp_ids(co: str | None = None) -> set[int] | None:
    """그 범위의 직원 번호들. 통합이면 None (거르지 않음)."""
    cond = sql("", co)
    if not cond:
        return None
    return {r["id"] for r in query("SELECT id FROM employees WHERE 1=1" + cond)}


def keep(rows: list, field: str = "employee_id", co: str | None = None) -> list:
    """직원 번호가 들어 있는 줄 목록을 지금 범위의 직원만 남김."""
    ids = emp_ids(co)
    if ids is None:
        return rows
    return [r for r in rows if r.get(field) in ids]


def keep_co(rows: list, field: str = "company", co: str | None = None) -> list:
    """company 칸이 있는 줄 목록을 지금 범위만 남김."""
    c = cur() if co is None else co
    if c not in IDS:
        return rows
    return [r for r in rows if norm(r.get(field)) == c]


def of_employee(emp_id) -> str:
    row = query_one("SELECT company FROM employees WHERE id=?", (emp_id,)) if emp_id else None
    return norm(row["company"]) if row else "1"


def name_for(emp) -> str:
    """직원(자료 또는 번호)이 속한 회사의 이름. 메일·통지서에 씁니다."""
    s = get_settings()
    if not enabled(s):
        return name("1", s)
    cid = norm(emp.get("company")) if isinstance(emp, dict) and "company" in emp else of_employee(emp.get("id") if isinstance(emp, dict) else emp)
    return name(cid, s)


def label(value, s: dict | None = None) -> str:
    """자료에 적힌 회사 값 → 짧은 이름 (자회사를 켜지 않았으면 빈 글자)."""
    s = get_settings() if s is None else s
    return short(norm(value), s) if enabled(s) else ""


def tabs(path: str, params) -> dict:
    """화면 위 회사 탭. 지금 주소의 다른 조건(연도 등)은 그대로 두고 co 만 바꿉니다."""
    from urllib.parse import urlencode
    s = get_settings()
    if not enabled(s):
        return {"show": False}
    keepq = [(k, v) for k, v in params.multi_items() if k not in ("co", "msg")] if hasattr(params, "multi_items") else []
    c = cur()
    items = [{"id": "all", "name": "통합", "active": c == "all"}] + \
            [{"id": x["id"], "name": x["short"], "active": c == x["id"]} for x in companies(s)]
    for it in items:
        it["url"] = path + "?" + urlencode(keepq + [("co", it["id"])])
    return {"show": True, "items": items, "cur": c, "cur_name": next(i["name"] for i in items if i["active"])}
