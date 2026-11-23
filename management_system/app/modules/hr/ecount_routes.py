"""이카운트 연동 화면 (/hr/ecount) · 직원별 급여·연말정산 정보 · 입력 도우미용 자료."""
import io
import json
import os
import zipfile
from urllib.parse import quote

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.common import templates, today
from app.db import audit, dept_tree, get_settings, query_one, set_setting
from app import company
from app.modules.hr import ecount, ecount_robot

router = APIRouter()

DATE_FORMATS = ["YYYY-MM-DD", "YYYYMMDD", "YYYY/MM/DD"]
MAX_TEMPLATE_BYTES = 2 * 1024 * 1024


def _redirect(url: str, msg: str = "") -> RedirectResponse:
    if msg:
        url += ("&" if "?" in url else "?") + "msg=" + quote(msg)
    return RedirectResponse(url, status_code=303)


def _keys(values) -> list[tuple[int, str]]:
    """'12:신규' 형식 → [(12, '신규')]"""
    out = []
    for v in values:
        emp, _, act = str(v).partition(":")
        if emp.isdigit() and act in ("신규", "퇴사"):
            out.append((int(emp), act))
    return out


def _int_or_none(v, lo=0, hi=10**10):
    v = str(v or "").replace(",", "").strip()
    if not v:
        return None
    if not v.isdigit() or not lo <= int(v) <= hi:
        raise ValueError
    return int(v)


@router.get("/hr/ecount", response_class=HTMLResponse)
def ecount_home(request: Request, tab: str = "pending"):
    rows = ecount.pending()
    t = ecount.get_template()
    s = get_settings()
    return templates.TemplateResponse(request, "hr/ecount.html", {
        "active_menu": "hr.employees", "msg": request.query_params.get("msg", ""), "tab": tab,
        "new_rows": [r for r in rows if r["action"] == "신규"], "out_rows": [r for r in rows if r["action"] == "퇴사"],
        "history": ecount.history() if tab == "history" else [],
        "tpl": t, "targets": ecount.all_targets(), "pay_items": ",".join(ecount.pay_items()),
        "date_format": s.get("ecount_date_format") or "YYYY-MM-DD", "date_formats": DATE_FORMATS,
        **_robot_ctx(),
    })


def _rc() -> str:
    """로봇 탭이 다루는 회사: 위의 회사 탭이 자회사면 자회사, 아니면 본사."""
    return "2" if company.cur() == "2" else "1"


def _robot_ctx(new_key: str = "") -> dict:
    rc = _rc()
    return {"jobs": ecount_robot.latest_jobs(), "robot": ecount_robot.status(rc), "robot_new_key": new_key, "rc": rc,
            "robots": {c: ecount_robot.status(c) for c in company.IDS},
            "ec_depts": ecount_robot.ecount_depts(rc), "dept_map": ecount_robot.dept_map(rc), "bms_depts": dept_tree(),
            "pay_std": ecount_robot.pay_std(rc), "ok_confirms": ", ".join(ecount_robot.ok_confirms(rc))}


def _who(request: Request) -> str:
    u = request.state.user
    return (u or {}).get("username") or ""


# ── 로봇: 담당자 화면 ──────────────────────────────────────────────────
@router.post("/hr/ecount/robot/request")
async def robot_request(request: Request):
    f = await request.form()
    keys = _keys(f.getlist("keys"))
    if not keys:
        return _redirect("/hr/ecount", "이카운트에 넣을 직원을 고르세요")
    try:
        n = ecount_robot.request(keys, _who(request))
    except ecount_robot.RobotError as ex:
        return _redirect("/hr/ecount?tab=robot", str(ex))
    on = all(ecount_robot.status(c)["online"] for c in {company.of_employee(e) for e, _a in keys})
    tail = "" if on else " 로봇이 꺼져 있는 것 같습니다. 관리 PC를 확인하세요."
    return _redirect("/hr/ecount", (f"{n}명을 로봇에 요청했습니다. 1~3분 뒤 결과가 표시됩니다." if n
                                    else "이미 요청했거나 대기 중이 아닌 직원입니다.") + tail)


@router.post("/hr/ecount/robot/cancel/{job_id}")
def robot_cancel(request: Request, job_id: int):
    ok = ecount_robot.cancel(job_id, _who(request))
    return _redirect("/hr/ecount", "요청을 취소했습니다" if ok else "이미 로봇이 처리 중이라 취소할 수 없습니다")


@router.post("/hr/ecount/robot/key", response_class=HTMLResponse)
def robot_key(request: Request):
    if (request.state.user or {}).get("role") != "admin":
        return _redirect("/hr/ecount?tab=robot", "로봇 열쇠는 관리자만 만들 수 있습니다")
    key = ecount_robot.new_key(_rc())
    rows = ecount.pending()
    s = get_settings()
    return templates.TemplateResponse(request, "hr/ecount.html", {
        "active_menu": "hr.employees", "msg": "", "tab": "robot",
        "new_rows": [r for r in rows if r["action"] == "신규"], "out_rows": [r for r in rows if r["action"] == "퇴사"],
        "history": [], "tpl": ecount.get_template(), "targets": ecount.all_targets(), "pay_items": ",".join(ecount.pay_items()),
        "date_format": s.get("ecount_date_format") or "YYYY-MM-DD", "date_formats": DATE_FORMATS, **_robot_ctx(key)})


@router.post("/hr/ecount/robot/settings")
async def robot_settings(request: Request):
    f = await request.form()
    rc = company.norm(f.get("rc")) if company.enabled() else "1"      # 화면이 보여 준 회사의 설정을 저장
    ecount_robot.save_dept_map({k[5:]: str(v).strip() for k, v in f.items() if k.startswith("dept:")}, rc)
    ps = str(f.get("pay_std") or "").strip()
    if ps and len(ps) <= 20:
        set_setting(company.key("ecount_pay_std", rc), ps)
    set_setting(company.key("ecount_ok_confirms", rc), str(f.get("ok_confirms") or "").strip()[:300])
    return _redirect("/hr/ecount?tab=robot", "로봇 설정을 저장했습니다")


# ── 로봇: 프로그램이 부르는 통로 (로그인 없이 열쇠로만) ────────────────────
def _robot_guard(request: Request) -> JSONResponse | str:
    """막을 때는 응답, 통과면 그 열쇠의 회사('1'/'2')."""
    from app.security.middleware import _proxied, is_https
    peer = request.client.host if request.client else ""
    local = peer in ("127.0.0.1", "::1", "testclient") and not _proxied(request)       # 개발·시험용 직접 접속
    if not (is_https(request) or local):
        return JSONResponse({"ok": False, "message": "HTTPS 로만 쓸 수 있습니다"}, status_code=403)
    co = ecount_robot.key_company(request.headers.get("x-robot-key"))
    if not co:
        audit("security", "이카운트 로봇 열쇠 불일치", getattr(request.state, "ip", ""), "")
        return JSONResponse({"ok": False, "message": "열쇠가 맞지 않습니다"}, status_code=403)
    return co


async def _robot_body(request: Request) -> dict:
    try:
        b = await request.json()
    except ValueError:
        b = {}
    return b if isinstance(b, dict) else {}


@router.post("/robot/ecount/claim")
async def robot_claim(request: Request):
    co = _robot_guard(request)
    if not isinstance(co, str):
        return co
    return JSONResponse({"ok": True, "job": ecount_robot.claim(co)}, headers={"Cache-Control": "no-store"})


@router.post("/robot/ecount/finish")
async def robot_finish(request: Request):
    co = _robot_guard(request)
    if not isinstance(co, str):
        return co
    b = await _robot_body(request)
    jid = b.get("job_id")
    done = ecount_robot.finish(int(jid) if str(jid).isdigit() else 0, bool(b.get("ok")), str(b.get("message") or ""),
                               b.get("detail") if isinstance(b.get("detail"), dict) else {}, co)
    return JSONResponse({"ok": done})


@router.post("/robot/ecount/depts")
async def robot_depts(request: Request):
    co = _robot_guard(request)
    if not isinstance(co, str):
        return co
    b = await _robot_body(request)
    ecount_robot.touch(co)
    return JSONResponse({"ok": True, "saved": ecount_robot.save_ecount_depts(b.get("depts"), co)})


@router.post("/hr/ecount/export")
async def ecount_export(request: Request):
    f = await request.form()
    keys = _keys(f.getlist("keys"))
    if not keys:
        return _redirect("/hr/ecount", "엑셀에 넣을 직원을 고르세요")
    include_rrn = f.get("include_rrn") == "1"
    ids = []
    for emp_id, _act in keys:
        if emp_id not in ids:
            ids.append(emp_id)
    header, rows = ecount.export_rows(ids, include_rrn)
    data = ecount.build_xlsx(header, rows)
    audit("hr", "개인정보 내보내기", "이카운트 사원등록 엑셀",
          f"{len(rows)}명" + (", 주민번호 포함" if include_rrn else ""))
    name = f"이카운트_사원등록_{today().strftime('%Y%m%d')}.xlsx"
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
                             "Cache-Control": "no-store"})


@router.post("/hr/ecount/mark")
async def ecount_mark(request: Request):
    f = await request.form()
    keys = _keys(f.getlist("keys"))
    status = "제외" if f.get("status") == "제외" else "완료"
    method = f.get("method") if f.get("method") in ("엑셀", "직접") else "엑셀"
    n = ecount.mark(keys, status, method if status == "완료" else "", (f.get("note") or "").strip()[:200])
    return _redirect("/hr/ecount", f"{n}건을 '{status}'(으)로 표시했습니다" if n else "표시할 항목을 고르세요")


@router.post("/hr/ecount/history/{sync_id}/undo")
def ecount_undo(sync_id: int):
    ecount.unmark(sync_id)
    return _redirect("/hr/ecount?tab=history", "반영 대기로 되돌렸습니다")


# ── 양식·설정 ──────────────────────────────────────────────────────────
@router.post("/hr/ecount/template")
async def ecount_template_upload(file: UploadFile = File(...)):
    data = await file.read(MAX_TEMPLATE_BYTES + 1)
    if len(data) > MAX_TEMPLATE_BYTES:
        return _redirect("/hr/ecount?tab=template", "양식 파일이 너무 큽니다 (2MB 이하)")
    try:
        t = ecount.read_template(data, file.filename or "")
    except ecount.TemplateError as ex:
        return _redirect("/hr/ecount?tab=template", str(ex))
    ecount.save_template(t)
    missing = sum(1 for c in t["columns"] if not c["field"])
    return _redirect("/hr/ecount?tab=template", f"양식 칸 {len(t['columns'])}개를 읽었습니다"
                     + (f" — 짝을 못 찾은 {missing}칸을 확인하세요" if missing else ""))


@router.post("/hr/ecount/template/map")
async def ecount_template_map(request: Request):
    f = await request.form()
    t = ecount.get_template()
    valid = {k for k, *_ in ecount.all_targets()}
    for i, c in enumerate(t["columns"]):
        v = f.get(f"field_{i}", "")
        c["field"] = v if v in valid else ""
    t.pop("default", None)
    ecount.save_template(t)
    return _redirect("/hr/ecount?tab=template", "칸 짝을 저장했습니다")


@router.post("/hr/ecount/template/reset")
def ecount_template_reset():
    set_setting("ecount_template", "")
    audit("hr", "이카운트 양식 초기화", "", "")
    return _redirect("/hr/ecount?tab=template", "기본 칸 구성으로 되돌렸습니다")


@router.post("/hr/ecount/settings")
async def ecount_settings(request: Request):
    f = await request.form()
    items = [x.strip() for x in str(f.get("pay_items") or "").replace("\n", ",").split(",") if x.strip()]
    if not items or len(items) > 30 or any(len(x) > 20 for x in items):
        return _redirect("/hr/ecount?tab=template", "급여항목 이름을 쉼표로 구분해 30개 이하로 적어 주세요")
    set_setting("ecount_pay_items", ",".join(dict.fromkeys(items)))
    if f.get("date_format") in DATE_FORMATS:
        set_setting("ecount_date_format", f.get("date_format"))
    audit("hr", "이카운트 설정 저장", "", "")
    return _redirect("/hr/ecount?tab=template", "설정을 저장했습니다")


# ── 입력 도우미 ────────────────────────────────────────────────────────
@router.get("/hr/ecount/helper.zip")
def ecount_helper_zip(request: Request):
    src = ecount.helper_files_dir()
    buf = io.BytesIO()
    base = str(request.base_url).rstrip("/")
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(os.listdir(src)):
            p = os.path.join(src, name)
            if os.path.isfile(p) and not name.startswith(".") and not name.endswith(".pyc"):
                z.write(p, f"bms_ecount_helper/{name}")
        z.writestr("bms_ecount_helper/config.json", json.dumps({"bms_url": base}, ensure_ascii=False, indent=2))
    audit("hr", "이카운트 입력 도우미 내려받기", "", "")
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": "attachment; filename=bms_ecount_helper.zip"})


@router.get("/hr/ecount/api/pending")
def ecount_api_pending(request: Request):
    """도우미가 대기 목록을 받습니다 (담당자가 같은 브라우저에서 BMS 에 로그인한 상태)."""
    rows = ecount.pending()
    sess = request.state.session
    return JSONResponse({"csrf": sess["csrf_token"] if sess else "", "items": [
        {"employee_id": r["id"], "action": r["action"], "name": r["name"], "emp_no": r["emp_no"],
         "department": r["department"] or "", "hire_date": r["hire_date"], "resign_date": r["resign_date"] or "",
         "warn": r["warn"]} for r in rows]}, headers={"Cache-Control": "no-store"})


@router.post("/hr/ecount/api/payload")
async def ecount_api_payload(request: Request):
    body = await request.json()
    emp_id, action = int(body.get("employee_id") or 0), body.get("action")
    if action not in ("신규", "퇴사"):
        return JSONResponse({"error": "잘못된 요청"}, status_code=400)
    p = ecount.helper_payload(emp_id, action)
    if not p:
        return JSONResponse({"error": "직원을 찾을 수 없습니다"}, status_code=404)
    audit("hr", "개인정보 내보내기", f"이카운트 입력 도우미 {p['emp_no']} {p['name']}",
          action + (", 주민번호 포함" if any(x["key"] == "rrn" for x in p["fields"]) else ""))
    return JSONResponse(p, headers={"Cache-Control": "no-store"})


@router.post("/hr/ecount/api/done")
async def ecount_api_done(request: Request):
    body = await request.json()
    emp_id, action = int(body.get("employee_id") or 0), body.get("action")
    if not query_one("SELECT 1 x FROM employees WHERE id=?", (emp_id,)):
        return JSONResponse({"error": "직원을 찾을 수 없습니다"}, status_code=404)
    n = ecount.mark([(emp_id, action)], "완료", "도우미", str(body.get("note") or "")[:200])
    return JSONResponse({"ok": True, "marked": n})


# ── 직원별 급여·연말정산 정보 (인사기록카드 탭) ───────────────────────────
@router.post("/hr/employees/{emp_id}/payroll")
async def payroll_save(request: Request, emp_id: int):
    if not query_one("SELECT 1 x FROM employees WHERE id=?", (emp_id,)):
        return _redirect("/hr/employees", "직원을 찾을 수 없습니다")
    f = await request.form()
    back = f"/hr/employees/{emp_id}?tab=payroll"
    try:
        data = {"householder": f.get("householder") if f.get("householder") in ("Y", "N") else None,
                "dependents": _int_or_none(f.get("dependents"), 0, 30),
                "children_8_20": _int_or_none(f.get("children_8_20"), 0, 20),
                "insured_monthly": _int_or_none(f.get("insured_monthly"), 0, 10**9),
                "pension_monthly": _int_or_none(f.get("pension_monthly"), 0, 10**9),
                "employ_monthly": _int_or_none(f.get("employ_monthly"), 0, 10**9),
                "note": (f.get("note") or "").strip()[:500],
                "pay_items": {}}
        for name in ecount.pay_items():
            v = _int_or_none(f.get(f"pay_{name}"), 0, 10**9)
            if v:
                data["pay_items"][name] = v
        annual = _int_or_none(f.get("sal_amount"), 1, 10**11)
    except ValueError:
        return _redirect(back, "숫자 칸에는 0 이상의 숫자만 적어 주세요")
    if data["children_8_20"] is not None and data["dependents"] is not None and data["children_8_20"] > data["dependents"]:
        return _redirect(back, "8~20세 자녀 수가 부양가족 수보다 많습니다")
    ecount.save_payroll(emp_id, data)
    if annual:  # 올해 연봉 칸(급여항목 합계 × 12 로 자동 입력, 고칠 수 있음) → 값이 달라졌을 때만 연봉에 반영
        from app.db import get_codes
        from app.modules.hr import repo
        year = today().year
        cur = next((x for x in repo.employee_salaries(emp_id) if x["year"] == year), None)
        if not cur or int(cur["annual_salary"]) != annual:
            reasons = get_codes("SALARY_REASON")
            repo.upsert_salary(emp_id, year, annual, (cur or {}).get("effective_date"),
                               (cur or {}).get("reason") or (reasons[0] if reasons else "정기인상"), (cur or {}).get("note") or "")
            audit("hr", "연봉 저장", f"emp#{emp_id}", f"{year}년 (급여·연말정산 탭)")
    return _redirect(back, "급여·연말정산 정보를 저장했습니다")
