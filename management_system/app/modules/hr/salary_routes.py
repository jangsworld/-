"""개인별 연봉 현황 화면 (/hr/salaries/person/…) · 과거 연봉 일괄 올리기. 관리자·인사담당만 (middleware RULES)."""
from urllib.parse import quote

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from app.common import templates, today
from app.db import audit
from app.modules.hr import salary_person as sp

router = APIRouter(prefix="/hr/salaries")


def _go(url: str, msg: str = "") -> RedirectResponse:
    if msg:
        url += ("&" if "?" in url else "?") + "msg=" + quote(msg)
    return RedirectResponse(url, status_code=303)


@router.get("/person/{emp_id}", response_class=HTMLResponse)
def person_page(request: Request, emp_id: int):
    v = sp.view(emp_id)
    if not v:
        return _go("/hr/salaries?tab=people", "직원을 찾을 수 없습니다")
    audit("hr", "개인별 연봉 현황 열람", f"emp#{emp_id}", "")
    return templates.TemplateResponse(request, "hr/salary_person.html", {
        **v, "this_year": today().year, "active_menu": "hr.salaries", "msg": request.query_params.get("msg", "")})


@router.post("/person/{emp_id}")
async def person_save(request: Request, emp_id: int):
    f = await request.form()
    back = f"/hr/salaries/person/{emp_id}"
    values, steps = {}, {}
    try:
        for k, v in f.items():
            if k.startswith("y_") and k[2:].isdigit() and sp.MIN_YEAR <= int(k[2:]) <= today().year + 1:
                values[int(k[2:])] = sp.parse_amount(v)
            elif k.startswith("st_") and k[3:].isdigit():
                steps[int(k[3:])] = sp.parse_amount(v)
        r = sp.save_years(emp_id, values)        # 금액이 모두 맞을 때만 저장
        s = sp.save_steps(emp_id, steps)
    except sp.SalaryError as ex:
        return _go(back, str(ex))
    return _go(back, f"저장했습니다 (새로 {r['added']} · 고침 {r['changed'] + s['changed']} · 삭제 {r['removed'] + s['removed']})")


@router.post("/person/{emp_id}/change")
async def person_change(request: Request, emp_id: int):
    """같은 해에 연봉이 한 번 더 바뀐 경우: 적용일·금액을 한 건 추가."""
    f = await request.form()
    back = f"/hr/salaries/person/{emp_id}"
    try:
        res = sp.add_change(emp_id, str(f.get("effective_date") or ""), sp.parse_amount(f.get("annual_salary")), str(f.get("reason") or ""))
    except sp.SalaryError as ex:
        return _go(back, str(ex))
    return _go(back, {"added": "연봉 변경을 추가했습니다", "same": "같은 적용일에 같은 금액이 이미 있습니다",
                      "kept": "같은 적용일의 금액이 이미 있습니다. 표에서 그 칸의 금액을 직접 고쳐 주세요"}.get(res, "저장했습니다"))


@router.get("/history/template.csv")
def history_template(kind: str = ""):
    changes = kind == "changes"
    audit("hr", "과거 연봉 양식 내려받기", "적용일 양식" if changes else "", "")
    fname = quote(f"연봉{'변경_적용일' if changes else '이력_일괄입력'}_{today().isoformat()}.csv")
    return StreamingResponse(iter(["﻿" + (sp.changes_template_csv() if changes else sp.template_csv())]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f"attachment; filename*=UTF-8''{fname}"})


@router.post("/history/import")
async def history_import(file: UploadFile = File(...), overwrite: str = Form("")):
    back = "/hr/salaries?tab=people"
    raw = await file.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        return _go(back, "파일이 너무 큽니다 (최대 2MB)")
    for enc in ("utf-8-sig", "cp949"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        return _go(back, "파일 인코딩을 확인해 주세요 (엑셀에서 'CSV UTF-8'로 저장)")
    return _go(back, "과거 연봉 올리기 — " + await run_in_threadpool(sp.import_csv, text, bool(overwrite)))
