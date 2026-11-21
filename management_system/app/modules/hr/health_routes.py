"""건강검진 화면 (/hr/health). 관리자·인사담당만 (middleware RULES)."""
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.common import templates, today
from app.db import audit, query
from app.modules.hr import health

router = APIRouter(prefix="/hr/health")


def _year(v) -> int:
    try:
        y = int(v)
    except (TypeError, ValueError):
        return today().year
    return y if 2000 <= y <= 2100 else today().year


def _back(f, msg: str) -> RedirectResponse:
    q = {k: str(f.get(k) or "") for k in ("year", "department", "q", "show") if f.get(k)}
    return RedirectResponse("/hr/health?" + urlencode({**q, "msg": msg}), status_code=303)


@router.get("", response_class=HTMLResponse)
def page(request: Request, year: int | None = None, department: str = "", q: str = "", show: str = ""):
    y = _year(year)
    ov = health.overview(y, department, q)
    audit("hr", "건강검진 현황 열람", f"{y}년", department)
    rows = [r for r in ov["rows"] if show != "todo" or r["status"] == "todo"]
    t = today()
    return templates.TemplateResponse(request, "hr/health.html", {
        "year": y, "years": list(range(t.year + 1, t.year - 4, -1)), "ov": ov, "rows": rows,
        "f": {"department": department, "q": q.strip(), "show": show},
        "depts": [r["department"] for r in query("SELECT DISTINCT department FROM employees WHERE department<>'' ORDER BY department")],
        "kinds": health.KINDS, "cycles": health.CYCLES, "default_cycle": health.default_cycle(),
        "today_iso": t.isoformat(), "active_menu": "hr.health", "msg": request.query_params.get("msg", ""),
    })


@router.post("/save")
async def save(request: Request):
    f = await request.form()
    data = {k: f.get(k) for k in ("id", "check_date", "kind", "institution", "note")}
    data["employee_ids"] = f.getlist("employee_ids")
    try:
        n = health.save(data)
    except health.HealthError as ex:
        return _back(f, str(ex))
    return _back(f, "고쳤습니다" if data.get("id") else (f"{n}명 등록했습니다" if n else "이미 같은 날 같은 검진이 등록되어 있습니다"))


@router.post("/{rid}/delete")
async def delete(rid: int, request: Request):
    f = await request.form()
    health.delete(rid)
    return _back(f, "삭제했습니다")


@router.post("/cycle")
async def cycle(request: Request):
    f = await request.form()
    try:
        if f.get("default_cycle"):
            health.set_default_cycle(str(f.get("default_cycle")))
            return _back(f, "기본 주기를 바꿨습니다")
        ids = [int(x) for x in f.getlist("employee_ids") if str(x).isdigit()]
        if not ids:
            return _back(f, "직원을 고르세요")
        n = health.set_cycle(ids, str(f.get("cycle") or ""))
    except health.HealthError as ex:
        return _back(f, str(ex))
    return _back(f, f"{n}명의 검진 주기를 바꿨습니다")
