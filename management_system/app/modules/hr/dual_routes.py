"""프로젝트 이중가입 화면 (/hr/dual). 관리자·인사담당만 (middleware RULES)."""
import csv
import io
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from app.common import templates, today
from app.db import audit, query
from app import company
from app.modules.hr import dual

router = APIRouter(prefix="/hr/dual")
STATES = ["진행중", "예정", "종료", "정리필요"]


def _go(msg: str = "", state: str = "", open_id: int = 0) -> RedirectResponse:
    url = "/hr/dual" + (f"?state={quote(state)}" if state in STATES else "")
    if open_id:                                   # 체크한 줄은 펼친 채로 돌아옴
        url += ("&" if "?" in url else "?") + f"open={open_id}"
    if msg:
        url += ("&" if "?" in url else "?") + "msg=" + quote(msg)
    return RedirectResponse(url, status_code=303)


@router.get("", response_class=HTMLResponse)
def dual_page(request: Request, state: str = "", emp: int = 0, open: int = 0):
    state = state if state in STATES else ""
    everything = dual.rows()
    rows = dual.rows(state)
    t = today().isoformat()
    return templates.TemplateResponse(request, "hr/dual.html", {
        "rows": rows, "sum": dual.summary(everything), "state": state, "states": STATES, "open_emp": emp, "open_id": open,
        "reasons": dual.REASONS, "insurances": dual.INSURANCES, "pay_types": dual.PAY_TYPES, "today_iso": t,
        "emps": [e for e in query("SELECT id, name, emp_no, department, resign_date FROM employees WHERE 1=1" + company.sql("") + " ORDER BY name")
                 if not e.get("resign_date") or e["resign_date"] >= t],
        "active_menu": "hr.dual", "msg": request.query_params.get("msg", ""),
    })


@router.post("/save")
async def dual_save(request: Request):
    f = dict(await request.form())
    try:
        dual.save(f)
    except dual.DualError as ex:
        return _go(str(ex), f.get("state") or "")
    return _go("저장했습니다", f.get("state") or "")


@router.post("/{did}/delete")
def dual_delete(did: int):
    dual.delete(did)
    return _go("삭제했습니다")


@router.post("/{did}/check")
async def dual_check(request: Request, did: int):
    f = await request.form()
    dual.toggle_check(did, str(f.get("key") or ""))
    return _go("", str(f.get("state") or ""), did)


@router.post("/{did}/apply-bases")
async def dual_apply(request: Request, did: int):
    f = await request.form()
    try:
        amount = dual.apply_bases(did, str(f.get("which") or ""))
    except dual.DualError as ex:
        return _go(str(ex))
    return _go(f"건강보험·국민연금·고용보험 기준 금액을 {amount:,}원으로 바꿨습니다. 국민연금 상한액은 급여·연말정산 탭에서 확인하세요",
               str(f.get("state") or ""))


@router.get("/export.csv")
def dual_export(state: str = ""):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["상태", "부서", "사번", "성명", "상대 사업장", "프로젝트", "발주처", "시작일", "종료 예정일", "실제 종료일", "사유",
                "이중가입 보험", "급여 형태", "남은 처리"])
    for r in dual.rows(state if state in STATES else ""):
        w.writerow([r["state"], r.get("department") or "", r.get("emp_no") or "", r["name"], r["workplace"], r.get("project") or "",
                    r.get("client") or "", r["start_date"], r.get("end_date") or "", r.get("actual_end") or "",
                    (r.get("reason") or "") + (f" ({r['reason_note']})" if r.get("reason_note") else ""),
                    "·".join(r["insurances"]), r["pay_type"], ", ".join(r["todo_left"])])      # 금액은 내보내지 않음
    audit("hr", "이중가입 현황 내려받기", state or "전체", "")
    fname = quote(f"프로젝트_이중가입_{today().isoformat()}.csv")
    return StreamingResponse(iter(["﻿" + buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f"attachment; filename*=UTF-8''{fname}"})
