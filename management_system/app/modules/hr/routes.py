"""인사관리 페이지 및 API 라우트 (prefix: /hr)."""
import csv
import io
import json
import os
from urllib.parse import quote

from fastapi import APIRouter, Request, HTTPException, UploadFile, File
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse, Response

from app.common import templates, today, sanitize_html, add_months, days_until, to_date
from app.db import get_codes, get_settings, audit
from app import mailer
from app.config import settings as cfg
from app.modules.hr import intake, repo, rrn as rrn_util, services, signing
from app.modules.hr.constants import (
    QUALIFICATION_KINDS, CONTRACT_CATEGORIES, CERT_CATEGORIES, CONTRACT_STATUSES,
    DISCIPLINE_EXPUNGE_MONTHS, WORK_SPECIALS, PLACEHOLDERS,
)

router = APIRouter(prefix="/hr")


def _cell(v) -> str:
    """CSV 수식 주입(=, +, -, @) 방지: 엑셀에서 수식으로 실행되지 않도록 작은따옴표 접두."""
    if isinstance(v, (int, float)):
        return str(v)
    v = "" if v is None else str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


def _csv_response(rows: list[list], filename: str) -> StreamingResponse:
    buf = io.StringIO()
    w = csv.writer(buf)
    for r in rows:
        w.writerow([_cell(x) for x in r])
    return StreamingResponse(iter(["\ufeff" + buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"})


def _redirect(url: str, msg: str = "") -> RedirectResponse:
    if msg:
        url += ("&" if "?" in url else "?") + "msg=" + quote(msg)
    return RedirectResponse(url, status_code=303)


def _codes() -> dict:
    return {
        "depts": get_codes("DEPT"), "ranks": get_codes("RANK"), "titles": get_codes("TITLE"),
        "locations": get_codes("LOCATION"), "employment_types": get_codes("EMPLOYMENT_TYPE"),
        "emergency_relations": get_codes("EMERGENCY_RELATION"), "edu_levels": get_codes("EDU_LEVEL"),
        "qualification_kinds": QUALIFICATION_KINDS, "qualification_grades": get_codes("QUAL_GRADE"),
    }


def _reward_categories() -> dict:
    return {"포상": get_codes("REWARD_포상"), "징계": get_codes("REWARD_징계")}


def _render(request: Request, name: str, ctx: dict, active: str = ""):
    ctx.setdefault("active_menu", active)
    ctx.setdefault("msg", request.query_params.get("msg", ""))
    return templates.TemplateResponse(request, name, ctx)


def _emp_or_404(emp_id: int) -> dict:
    e = repo.get_employee(emp_id)
    if not e:
        raise HTTPException(404, "임직원을 찾을 수 없습니다")
    return e


@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
def hr_root():
    return RedirectResponse("/hr/dashboard")


# ════════════════════════════════════════════════════════════════════════
# 0. 대시보드
# ════════════════════════════════════════════════════════════════════════
@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    d = services.dashboard()
    if request.state.user["role"] not in ("admin", "hr"):  # 조회자·총무담당: 연봉·알림·상벌·휴직 정보는 응답에서 제거
        d["alerts"], d["recent_rewards"], d["active_specials"] = [], [], []
        d["charts"]["salary_trend"] = []
        for k in ("avg_salary", "payroll", "contract_missing"):
            d["kpi"][k] = 0
    return _render(request, "hr/dashboard.html", {"d": d}, "hr.dashboard")


# ════════════════════════════════════════════════════════════════════════
# 1. 임직원 현황
# ════════════════════════════════════════════════════════════════════════
@router.get("/employees", response_class=HTMLResponse)
def employees(request: Request, q: str = "", department: str = "", employment_type: str = "",
              status: str = "재직(전체)"):
    rows = repo.list_employees(q, department, employment_type, status)
    codes = _codes()
    known = [t for t in codes["employment_types"] if t != "기타"]
    codes["employment_types"] = known + [t for t in repo.employment_types_in_use() if t not in known]
    is_staff = request.state.user["role"] in ("admin", "hr")
    return _render(request, "hr/employees.html", {
        "rows": rows, "f": {"q": q, "department": department, "employment_type": employment_type,
                            "status": status},
        "drafts": [_draft_row(d) for d in repo.list_drafts("확인필요")] if is_staff else [],
        "intake": {"enabled": bool(cfg.employee_root), **intake.state},
        **codes,
    }, "hr.employees")


CSV_COLUMNS = [
    ("emp_no", "사번"), ("name", "성명"), ("birth_date", "생년월일"), ("gender", "성별"),
    ("phone", "연락처"), ("email", "이메일"), ("address", "주소"), ("emergency_contact", "비상연락처"),
    ("emergency_relation", "비상연락처 관계"),
    ("department", "부서"), ("rank", "직급"), ("title", "직책"), ("job_duty", "담당업무"),
    ("employment_type", "고용형태"), ("work_location", "근무지"), ("hire_date", "입사일"),
    ("probation_end", "수습종료일"), ("resign_date", "퇴사일"), ("resign_reason", "퇴사사유"),
    ("bank_name", "급여은행"), ("bank_account", "계좌번호"), ("edu_level", "최종학력"),
    ("edu_school", "학교명"), ("edu_major", "학과"), ("edu_grad_year", "졸업연도"),
    ("certificates", "기타자격"), ("memo", "비고"),
]


@router.get("/employees/export.csv")
def employees_export():
    rows = repo.list_employees(status="")
    out = [[k for _, k in CSV_COLUMNS] + ["상태", "현재연봉"]]
    for r in rows:
        out.append([r.get(f) or "" for f, _ in CSV_COLUMNS] + [r["status"], r.get("annual_salary") or ""])
    audit("hr", "개인정보 내보내기", "임직원현황 CSV", f"{len(rows)}명")
    return _csv_response(out, f"임직원현황_{today().isoformat()}.csv")


@router.get("/employees/template.csv")
def employees_csv_template():
    data = "﻿" + ",".join(k for _, k in CSV_COLUMNS) + "\n"
    fname = quote("임직원_일괄등록_양식.csv")
    return StreamingResponse(iter([data]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f"attachment; filename*=UTF-8''{fname}"})


@router.post("/employees/import")
async def employees_import(file: UploadFile = File(...)):
    raw = await file.read(5 * 1024 * 1024 + 1)
    if len(raw) > 5 * 1024 * 1024:
        return _redirect("/hr/employees", "파일이 너무 큽니다 (최대 5MB)")
    for enc in ("utf-8-sig", "cp949"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        return _redirect("/hr/employees", "파일 인코딩을 확인해주세요 (UTF-8 또는 CP949)")
    label_to_field = {k: f for f, k in CSV_COLUMNS}
    added = updated = skipped = 0
    for row in csv.DictReader(io.StringIO(text)):
        data = {label_to_field[k.strip()]: (v or "").strip() for k, v in row.items() if k and k.strip() in label_to_field}
        if not data.get("name") or not data.get("hire_date"):
            skipped += 1
            continue
        for dk in ("birth_date", "hire_date", "probation_end", "resign_date"):
            if data.get(dk):
                d = to_date(data[dk])
                data[dk] = d.isoformat() if d else None
        existing = None
        if data.get("emp_no"):
            existing = next((e for e in repo.list_employees(status="") if e["emp_no"] == data["emp_no"]), None)
        if existing:
            repo.update_employee(existing["id"], data)
            updated += 1
        else:
            repo.create_employee(data)
            added += 1
    audit("hr", "임직원 일괄등록", file.filename or "", f"신규 {added} / 수정 {updated}")
    return _redirect("/hr/employees", f"일괄등록 완료: 신규 {added}명 / 수정 {updated}명 / 제외 {skipped}건")


@router.get("/employees/new", response_class=HTMLResponse)
def employee_new(request: Request):
    return _render(request, "hr/employee_form.html", {
        "e": {"employment_type": "정규직", "hire_date": today().isoformat(), "emp_no": repo.next_emp_no()},
        "is_new": True, "quals": {}, **_codes(),
    }, "hr.employees")


def _employee_form(form: dict) -> tuple[dict, str | None, list[dict]]:
    """등록·수정 폼 공통 처리. (임직원 항목, 주민등록번호, 보유자격) 반환. 형식 오류는 ValueError."""
    data = dict(form)
    if data.get("employment_type") == "기타" and (data.get("employment_type_etc") or "").strip():
        data["employment_type"] = data["employment_type_etc"].strip()
    rrn = None
    if (data.get("rrn") or "").strip():
        rrn = rrn_util.normalize(data["rrn"])
        if not rrn:
            raise ValueError("주민등록번호 형식을 확인해주세요 (예: 000000-0000000)")
        parsed = rrn_util.parse(rrn)  # 생년월일·성별은 주민등록번호 기준으로 맞춤
        data["birth_date"], data["gender"] = parsed["birth_date"], parsed["gender"]
    quals = [{"kind": kind, "license_no": (form.get(f"q{i}_no") or "").strip(), "grade": (form.get(f"q{i}_grade") or "").strip(),
              "main_cert": (form.get(f"q{i}_cert") or "").strip(), "expires_on": (form.get(f"q{i}_exp") or "").strip()}
             for i, kind in enumerate(QUALIFICATION_KINDS)]
    return data, rrn, quals


def _save_extras(emp_id: int, rrn: str | None, quals: list[dict]):
    if rrn:
        repo.set_rrn(emp_id, rrn)
    repo.save_qualifications(emp_id, quals)


def _create_employee(form: dict, rrn: str | None, quals: list[dict]) -> int:
    emp_id = repo.create_employee(form)
    _save_extras(emp_id, rrn, quals)
    if form.get("initial_salary"):
        repo.upsert_salary(emp_id, to_date(form["hire_date"]).year, int(form["initial_salary"]),
                           form["hire_date"], "신규", "입사 시 책정")
    return emp_id


@router.post("/employees/new")
async def employee_create(request: Request):
    raw = dict(await request.form())
    if not raw.get("name") or not raw.get("hire_date"):
        return _redirect("/hr/employees/new", "성명과 입사일은 필수입니다")
    try:
        form, rrn, quals = _employee_form(raw)
        emp_id = _create_employee(form, rrn, quals)
    except ValueError as ex:
        return _redirect("/hr/employees/new", str(ex))
    except Exception as ex:  # 사번 중복 등
        return _redirect("/hr/employees/new", f"저장 실패: {ex}")
    return _redirect(f"/hr/employees/{emp_id}", "임직원이 등록되었습니다")


# ── 임직원 서류 폴더 → 등록 초안 ──
def _draft_row(d: dict) -> dict:
    data = d["data"] or {}
    return {**d, "fields": data.get("fields", {}), "checks": data.get("checks", []), "files": data.get("files", []),
            "rrn_masked": rrn_util.mask(data.get("rrn"))}


def _draft_or_404(draft_id: int) -> dict:
    d = repo.get_draft(draft_id)
    if not d or d["status"] != "확인필요":
        raise HTTPException(404, "확인 대기 중인 초안이 아닙니다")
    return d


@router.post("/drafts/scan")
def draft_scan():
    return _redirect("/hr/employees", intake.scan_once(force=True)["message"])


@router.get("/drafts/{draft_id}", response_class=HTMLResponse)
def draft_review(request: Request, draft_id: int):
    d = _draft_or_404(draft_id)
    data = d["data"] or {}
    fields = data.get("fields", {})
    salaries = data.get("salaries", [])
    audit("hr", "등록초안 열람", d["folder_name"], "")
    e = {"employment_type": "정규직", "hire_date": "", "emp_no": "", "initial_salary": salaries[0]["amount"] if salaries else "",
         "rrn_masked": rrn_util.mask(data.get("rrn")), **fields}
    e["emp_no"] = repo.next_emp_no(fields.get("hire_date"))
    return _render(request, "hr/employee_form.html", {
        "e": e, "is_new": True, "draft": d, "checks": data.get("checks", []), "sources": data.get("sources", {}),
        "files": data.get("files", []), "salaries": salaries,
        "quals": {q["kind"]: q for q in data.get("qualifications", [])}, **_codes(),
    }, "hr.employees")


@router.post("/drafts/{draft_id}")
async def draft_confirm(request: Request, draft_id: int):
    d = _draft_or_404(draft_id)
    data = d["data"] or {}
    raw = dict(await request.form())
    back = f"/hr/drafts/{draft_id}"
    if not raw.get("name") or not raw.get("hire_date"):
        return _redirect(back, "성명과 입사일은 필수입니다")
    try:
        form, rrn, quals = _employee_form(raw)
        rrn = rrn or data.get("rrn")
        if rrn and not raw.get("rrn"):
            parsed = rrn_util.parse(rrn)
            form["birth_date"], form["gender"] = parsed["birth_date"], parsed["gender"]
        form["folder_name"] = d["folder_name"]
        emp_id = _create_employee(form, rrn, quals)
    except ValueError as ex:
        return _redirect(back, str(ex))
    except Exception as ex:
        return _redirect(back, f"저장 실패: {ex}")
    hire_year = to_date(form["hire_date"]).year
    for s in data.get("salaries", []):
        if s["year"] != hire_year:
            repo.upsert_salary(emp_id, s["year"], s["amount"], s["date"], "조정", "입사서류 연봉계약서")
    repo.update_draft(draft_id, status="등록완료", employee_id=emp_id)
    return _redirect(f"/hr/employees/{emp_id}", "초안을 확인해 임직원으로 등록했습니다")


@router.post("/api/drafts/{draft_id}/rrn")
def draft_rrn_reveal(draft_id: int):
    """서류에서 읽은 주민등록번호를 원본과 대조하기 위한 확인 (접속기록 남김)."""
    d = _draft_or_404(draft_id)
    audit("hr", "주민등록번호 열람", f"초안 {d['folder_name']}", "")
    return {"ok": True, "rrn": (d["data"] or {}).get("rrn") or ""}


@router.post("/drafts/{draft_id}/exclude")
def draft_exclude(draft_id: int):
    d = _draft_or_404(draft_id)
    repo.update_draft(draft_id, status="제외")
    audit("hr", "등록초안 제외", d["folder_name"], "")
    return _redirect("/hr/employees", "초안을 제외했습니다. 해당 폴더는 다시 초안으로 만들지 않습니다")


@router.get("/employees/{emp_id}", response_class=HTMLResponse)
def employee_detail(request: Request, emp_id: int, tab: str = "info"):
    e = _emp_or_404(emp_id)
    if request.state.user["role"] not in ("admin", "hr") and tab in ("salary", "contract", "reward", "special", "files", "check"):
        tab = "info"
    audit("hr", "인사기록 열람", f"{e['emp_no']} {e['name']}", tab)  # 개인정보 접속기록
    files = intake.list_files(e) if tab == "files" else []
    return _render(request, "hr/employee_detail.html", {
        "e": e, "tab": tab, "files": files, "employee_root": cfg.employee_root,
        "doc_status": intake.document_status(get_codes("INTAKE_DOC"), files) if tab == "files" else [],
        "checklists": {"ONBOARD": ("입사", get_codes("ONBOARD")), "OFFBOARD": ("퇴사", get_codes("OFFBOARD"))},
        "checks": repo.list_checks(emp_id) if tab == "check" else {},
        "appointments": repo.list_appointments(emp_id),
        "contracts": repo.list_documents("contract", employee_id=emp_id),
        "certs": repo.list_documents("certificate", employee_id=emp_id),
        "salaries": repo.employee_salaries(emp_id),
        "rewards": repo.list_rewards(employee_id=emp_id),
        "specials": repo.list_specials(employee_id=emp_id),
        "appointment_types": get_codes("APPOINTMENT_TYPE"),
        "salary_reasons": get_codes("SALARY_REASON"),
        "reward_categories": _reward_categories(),
        "work_specials": WORK_SPECIALS,
        "expunge_months": DISCIPLINE_EXPUNGE_MONTHS,
        "cert_templates": repo.list_templates("certificate", active_only=True),
        "qualifications": repo.get_qualifications(emp_id), "qualification_kinds": QUALIFICATION_KINDS,
        "rrn_masked": repo.rrn_masked(emp_id) if request.state.user["role"] in ("admin", "hr") else "",
        "assets": __import__("app.modules.ga.repo", fromlist=["x"]).list_assets(holder_id=emp_id),
    }, "hr.employees")


@router.get("/employees/{emp_id}/edit", response_class=HTMLResponse)
def employee_edit(request: Request, emp_id: int):
    e = _emp_or_404(emp_id)
    e["rrn_masked"] = repo.rrn_masked(emp_id)
    return _render(request, "hr/employee_form.html", {"e": e, "is_new": False, "quals": repo.get_qualifications(emp_id),
                                                      **_codes()}, "hr.employees")


@router.post("/employees/{emp_id}/edit")
async def employee_update(request: Request, emp_id: int):
    _emp_or_404(emp_id)
    raw = dict(await request.form())
    appt_date = raw.pop("appt_date", None) or None
    try:
        form, rrn, quals = _employee_form(raw)
        repo.update_employee(emp_id, form, appt_date)
        _save_extras(emp_id, rrn, quals)
    except ValueError as ex:
        return _redirect(f"/hr/employees/{emp_id}/edit", str(ex))
    except Exception as ex:
        return _redirect(f"/hr/employees/{emp_id}/edit", f"저장 실패: {ex}")
    return _redirect(f"/hr/employees/{emp_id}", "저장되었습니다")


@router.get("/employees/{emp_id}/file")
def employee_file(emp_id: int, name: str):
    """임직원 폴더의 서류 열람. 폴더 목록에 있는 파일명만 허용합니다."""
    e = _emp_or_404(emp_id)
    if name not in {f["name"] for f in intake.list_files(e)}:
        raise HTTPException(404, "파일을 찾을 수 없습니다")
    audit("hr", "입사서류 열람", f"{e['emp_no']} {e['name']}", name)
    ext = name.rsplit(".", 1)[-1].lower()
    media = {"pdf": "application/pdf", "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}.get(ext)
    return FileResponse(os.path.join(intake.employee_folder(e), name), media_type=media or "application/octet-stream",
                        headers={"Content-Disposition": f"{'inline' if media else 'attachment'}; filename*=UTF-8''{quote(name)}"})


@router.post("/employees/{emp_id}/checks")
async def employee_check(request: Request, emp_id: int):
    _emp_or_404(emp_id)
    f = await request.form()
    kind = f.get("kind")
    if kind not in ("ONBOARD", "OFFBOARD"):
        raise HTTPException(400)
    repo.set_check(emp_id, kind, f.get("item", ""), bool(f.get("done")), request.state.user["display_name"])
    return _redirect(f"/hr/employees/{emp_id}?tab=check")


@router.get("/technicians", response_class=HTMLResponse)
def technicians(request: Request, kind: str = ""):
    rows = repo.list_technicians(kind)
    for r in rows:
        r["dday"] = days_until(r.get("expires_on"))
    return _render(request, "hr/technicians.html", {"rows": rows, "kind": kind, "kinds": QUALIFICATION_KINDS}, "hr.technicians")


@router.get("/technicians/export.csv")
def technicians_export(kind: str = ""):
    rows = repo.list_technicians(kind)
    out = [["구분", "성명", "사번", "부서", "직급", "자격번호", "등급", "대표 국가기술자격증", "교육·갱신 만료일"]]
    for r in rows:
        out.append([r["kind"], r["name"], r["emp_no"], r["department"], r["rank"], r["license_no"], r["grade"], r["main_cert"], r["expires_on"]])
    audit("hr", "개인정보 내보내기", "기술자 현황 CSV", f"{len(rows)}명")
    return _csv_response(out, f"기술자현황_{today().isoformat()}.csv")


@router.post("/api/employees/{emp_id}/rrn")
def employee_rrn_reveal(emp_id: int):
    """주민등록번호 원문 확인: 인사담당 이상, 버튼을 누를 때만, 접속기록 남김 (값은 기록하지 않음)."""
    e = _emp_or_404(emp_id)
    rrn = repo.get_rrn(emp_id)
    audit("hr", "주민등록번호 열람", f"{e['emp_no']} {e['name']}", "")
    return {"ok": True, "rrn": rrn or ""}


@router.post("/employees/{emp_id}/delete")
def employee_delete(emp_id: int):
    repo.delete_employee(emp_id)
    return _redirect("/hr/employees", "삭제되었습니다")


@router.post("/employees/{emp_id}/appointments")
async def appointment_add(request: Request, emp_id: int):
    f = dict(await request.form())
    repo.add_appointment(emp_id, f.get("appt_date") or today().isoformat(), f.get("appt_type", "기타"),
                         f.get("from_value"), f.get("to_value"), f.get("note"))
    return _redirect(f"/hr/employees/{emp_id}?tab=history", "발령 이력이 추가되었습니다")


@router.post("/appointments/{appt_id}/delete")
async def appointment_delete(request: Request, appt_id: int):
    f = dict(await request.form())
    repo.delete_appointment(appt_id)
    return _redirect(f.get("next") or "/hr/employees", "삭제되었습니다")


# ════════════════════════════════════════════════════════════════════════
# 2. 근로계약서 / 증명서
# ════════════════════════════════════════════════════════════════════════
@router.get("/contracts", response_class=HTMLResponse)
def contracts(request: Request, status: str = "", category: str = "", q: str = ""):
    docs = repo.list_documents("contract", status=status, category=category, q=q)
    for d in docs:
        d["dday"] = (to_date(d["end_date"]) - today()).days if to_date(d["end_date"]) else None
    active = repo.list_employees(status="재직(전체)")
    missing = [e for e in active if not e["has_contract"]]
    for e in missing:
        e["suggest"] = services.suggest_template_category(e)
    stat = {s: sum(1 for d in repo.list_documents("contract") if d["status"] == s) for s in CONTRACT_STATUSES}
    return _render(request, "hr/contracts.html", {
        "docs": docs, "missing": missing, "stat": stat,
        "statuses": CONTRACT_STATUSES, "categories": CONTRACT_CATEGORIES,
        "f": {"status": status, "category": category, "q": q},
        "coverage": (len(active) - len(missing)) / len(active) * 100 if active else 100,
    }, "hr.contracts")


@router.get("/contracts/new", response_class=HTMLResponse)
def contract_new(request: Request, employee_id: int | None = None, category: str = ""):
    emps = repo.list_employees(status="재직(전체)")
    emp = repo.get_employee(employee_id) if employee_id else None
    cat = category or (services.suggest_template_category(emp) if emp else "정규직")
    tpls = repo.list_templates("contract", active_only=True)
    params = services.default_contract_params(emp or {}, cat)
    return _render(request, "hr/contract_new.html", {
        "emps": emps, "emp": emp, "category": cat, "templates_": tpls, "p": params,
        "categories": CONTRACT_CATEGORIES,
        "suggest_map": {e["id"]: services.suggest_template_category(e) for e in emps},
    }, "hr.contracts")


def _create_contract(emp: dict, tpl: dict, params: dict, status="작성중") -> int:
    ctx = services.build_context(emp, params)
    content = services.render_template(tpl["content"], ctx)
    sal = int(params["annual_salary"]) if str(params.get("annual_salary") or "").isdigit() else None
    wh = float(params["weekly_hours"]) if params.get("weekly_hours") else None
    return repo.create_document(
        doc_group="contract", employee_id=emp["id"], template_id=tpl["id"], category=tpl["category"],
        title=f"{tpl['name']} - {emp['name']}", start_date=params.get("start_date") or None,
        end_date=params.get("end_date") or None, annual_salary=sal, weekly_hours=wh,
        params=json.dumps(params, ensure_ascii=False), content=content, status=status,
    )


@router.post("/contracts/new")
async def contract_create(request: Request):
    f = dict(await request.form())
    emp = _emp_or_404(int(f["employee_id"]))
    tpl = repo.get_template(int(f["template_id"]))
    if not tpl:
        return _redirect("/hr/contracts/new", "템플릿을 선택해주세요")
    params = {k: v for k, v in f.items() if k not in ("employee_id", "template_id")}
    doc_id = _create_contract(emp, tpl, params)
    if f.get("sync_salary") and params.get("annual_salary") and tpl["category"] == "연봉계약":
        sd = to_date(params.get("start_date")) or today()
        repo.upsert_salary(emp["id"], sd.year, int(params["annual_salary"]), sd.isoformat(), "정기인상", "연봉계약서 연동")
    return _redirect(f"/hr/documents/{doc_id}", "계약서가 자동 작성되었습니다. 내용을 검토·수정하세요")


@router.post("/contracts/bulk")
async def contract_bulk(request: Request):
    """미체결자 일괄 자동작성: 고용형태에 맞는 템플릿 + 현재 연봉으로 초안 생성."""
    form = await request.form()
    ids = [int(x) for x in form.getlist("employee_ids")]
    tpls = {t["category"]: t for t in repo.list_templates("contract", active_only=True)}
    n = 0
    for emp_id in ids:
        emp = repo.get_employee(emp_id)
        cat = services.suggest_template_category(emp)
        tpl = tpls.get(cat) or tpls.get("정규직")
        if not tpl:
            continue
        _create_contract(emp, tpl, services.default_contract_params(emp, cat))
        n += 1
    return _redirect("/hr/contracts?status=작성중", f"{n}건의 근로계약서 초안이 생성되었습니다")


@router.get("/documents/{doc_id}", response_class=HTMLResponse)
def document_editor(request: Request, doc_id: int):
    d = repo.get_document(doc_id)
    if not d:
        raise HTTPException(404, "문서를 찾을 수 없습니다")
    audit("hr", "문서 열람", f"doc#{doc_id}", d["title"])
    statuses = CONTRACT_STATUSES if d["doc_group"] == "contract" else ["발급완료", "발급취소"]
    sign = signing.latest_request(doc_id)
    sign_link = ""
    if sign and sign["status"] == "대기" and sign["expires_at"] > signing._now():
        sign_link = f"{signing.base_url(request)}/sign/{sign['token']}"
    return _render(request, "hr/document_editor.html", {
        "d": d, "statuses": statuses, "params": json.loads(d["params"] or "{}"),
        "sign": sign, "sign_link": sign_link, "locked": bool(sign and sign["status"] == "완료"),
        "mail_ready": mailer.configured(),
        "history": repo.list_documents(d["doc_group"], employee_id=d["employee_id"]),
    }, "hr.contracts" if d["doc_group"] == "contract" else "hr.certificates")


@router.post("/api/documents/{doc_id}")
async def document_save(doc_id: int, request: Request):
    body = await request.json()
    kw = {}
    if "content" in body and not signing.is_signed(doc_id):  # 전자서명 완료본은 본문 변경 불가
        kw["content"] = sanitize_html(body["content"])
    for k in ("status", "signed_date", "title", "start_date", "end_date"):
        if k in body:
            kw[k] = body[k] or None
    if kw.get("status") == "체결완료" and not kw.get("signed_date"):
        kw["signed_date"] = today().isoformat()
    repo.update_document(doc_id, **kw)
    audit("hr", "문서 저장", f"doc#{doc_id}", kw.get("status", ""))
    msg = "저장되었습니다"
    if kw.get("status") in ("체결완료", "발급완료") and cfg.pdf_on_signed:
        ok, m = await run_in_threadpool(services.store_document_pdf, doc_id)
        msg += " · " + m
    return {"ok": True, "message": msg}


@router.post("/documents/{doc_id}/pdf")
def document_pdf_store(doc_id: int):
    ok, m = services.store_document_pdf(doc_id)
    return _redirect(f"/hr/documents/{doc_id}", m)


@router.post("/documents/{doc_id}/sign-request")
def document_sign_request(request: Request, doc_id: int):
    """근로계약서 전자서명 요청: 메일 설정이 없으면 화면에 링크를 표시합니다."""
    d = repo.get_document(doc_id)
    if not d or d["doc_group"] != "contract":
        raise HTTPException(404, "계약서를 찾을 수 없습니다")
    back = f"/hr/documents/{doc_id}"
    emp = repo.get_employee(d["employee_id"])
    if not emp.get("email") or not emp.get("birth_date"):
        return _redirect(back, "임직원 정보에 이메일과 생년월일이 있어야 서명을 요청할 수 있습니다")
    if signing.is_signed(doc_id):
        return _redirect(back, "이미 전자서명이 완료된 문서입니다")
    if "ph-missing" in (d["content"] or ""):
        return _redirect(back, "비어 있는 항목이 있습니다. 문서를 완성한 뒤 요청하세요")
    _, mailed, error = signing.create_request(d, emp, signing.base_url(request))
    if mailed:
        return _redirect(back, "직원 메일로 전자서명 요청을 보냈습니다")
    return _redirect(back, f"{error or '메일 서버 미설정'}: 화면의 서명 링크를 직원에게 전달하세요")


@router.post("/documents/{doc_id}/email")
def document_email(doc_id: int):
    """발급한 증명서 등을 PDF 첨부로 직원 메일에 전송."""
    from app import storage
    d = repo.get_document(doc_id)
    if not d:
        raise HTTPException(404, "문서를 찾을 수 없습니다")
    back = f"/hr/documents/{doc_id}"
    if not mailer.configured():
        return _redirect(back, "메일 서버가 설정되지 않았습니다 (config/bms.toml [mail])")
    if not d.get("email"):
        return _redirect(back, "임직원 정보에 이메일이 없습니다")
    if not d.get("pdf_path"):
        ok, m = services.store_document_pdf(doc_id)
        if not ok:
            return _redirect(back, m)
        d = repo.get_document(doc_id)
    company = get_settings().get("company_name", "")
    try:
        pdf = storage.read_verified(d["pdf_path"], d["pdf_sha256"])
        mailer.send(d["email"], f"[{company}] {d['title']}",
                    f"{d['name']}님, 요청하신 문서를 첨부해 보내드립니다.\n\n{company} 경영관리팀",
                    [(f"{d['category']}_{d['name']}.pdf", pdf)])
    except (storage.StorageError, mailer.MailError) as ex:
        audit("hr", "메일 전송 실패", f"doc#{doc_id}", str(ex)[:120])
        return _redirect(back, str(ex))
    repo.update_document(doc_id, emailed_at=signing._now())
    audit("hr", "문서 메일 전송", f"doc#{doc_id}", d["title"])
    return _redirect(back, "직원 메일로 전송했습니다")


@router.get("/documents/{doc_id}/pdf")
def document_pdf_download(doc_id: int):
    from app import storage
    d = repo.get_document(doc_id)
    if not d or not d.get("pdf_path"):
        raise HTTPException(404, "보관된 PDF가 없습니다")
    try:
        data = storage.read_verified(d["pdf_path"], d["pdf_sha256"])
    except storage.StorageError as ex:
        raise HTTPException(409, str(ex))
    audit("hr", "PDF 열람", f"doc#{doc_id}", "")
    name = quote(f"{d['name']}_{d['category']}.pdf")
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f"inline; filename*=UTF-8''{name}"})


@router.post("/documents/{doc_id}/regenerate")
def document_regenerate(doc_id: int):
    d = repo.get_document(doc_id)
    if signing.is_signed(doc_id):
        return _redirect(f"/hr/documents/{doc_id}", "전자서명이 완료된 문서는 다시 작성할 수 없습니다")
    tpl = repo.get_template(d["template_id"]) if d and d["template_id"] else None
    if not tpl:
        return _redirect(f"/hr/documents/{doc_id}", "원본 템플릿이 없어 재생성할 수 없습니다")
    emp = repo.get_employee(d["employee_id"])
    content = services.render_template(tpl["content"], services.build_context(emp, json.loads(d["params"] or "{}")))
    repo.update_document(doc_id, content=content)
    return _redirect(f"/hr/documents/{doc_id}", "최신 임직원 정보로 다시 작성했습니다")


@router.post("/documents/{doc_id}/delete")
def document_delete(doc_id: int):
    d = repo.get_document(doc_id)
    repo.delete_document(doc_id)
    back = "/hr/certificates" if d and d["doc_group"] == "certificate" else "/hr/contracts"
    return _redirect(back, "삭제되었습니다")


# ── 템플릿 관리 ──
@router.get("/templates", response_class=HTMLResponse)
def template_list(request: Request):
    return _render(request, "hr/templates.html", {"tpls": repo.list_templates()}, "hr.templates")


@router.get("/templates/{tid}", response_class=HTMLResponse)
def template_edit(request: Request, tid: int):
    tpl = repo.get_template(tid) if tid else {
        "id": 0, "doc_group": "contract", "category": "정규직", "name": "", "content": "<h1 class=\"doc-title\">제목</h1>\n<p></p>",
        "is_active": 1,
    }
    if not tpl:
        raise HTTPException(404)
    return _render(request, "hr/template_editor.html", {
        "t": tpl, "placeholders": PLACEHOLDERS,
        "categories": {"contract": CONTRACT_CATEGORIES, "certificate": CERT_CATEGORIES},
    }, "hr.templates")


@router.post("/api/templates")
async def template_save(request: Request):
    b = await request.json()
    tid = repo.save_template(int(b.get("id") or 0) or None, b["doc_group"], b["category"], b["name"],
                             sanitize_html(b["content"]), 1 if b.get("is_active", True) else 0)
    return {"ok": True, "id": tid, "message": "템플릿이 저장되었습니다"}


@router.post("/templates/{tid}/delete")
def template_delete(tid: int):
    repo.delete_template(tid)
    return _redirect("/hr/templates", "템플릿이 삭제되었습니다")


# ── 증명서 발급 ──
@router.get("/certificates", response_class=HTMLResponse)
def certificates(request: Request, q: str = ""):
    return _render(request, "hr/certificates.html", {
        "docs": repo.list_documents("certificate", q=q), "q": q,
        "emps": repo.list_employees(status=""),
        "tpls": repo.list_templates("certificate", active_only=True),
    }, "hr.certificates")


@router.post("/certificates/new")
async def certificate_create(request: Request):
    f = dict(await request.form())
    emp = _emp_or_404(int(f["employee_id"]))
    tpl = repo.get_template(int(f["template_id"]))
    params = {"purpose": f.get("purpose", ""), "doc_no": repo.next_cert_no(), "job_duty": emp.get("job_duty")}
    content = services.render_template(tpl["content"], services.build_context(emp, params))
    doc_id = repo.create_document(
        doc_group="certificate", employee_id=emp["id"], template_id=tpl["id"], category=tpl["category"],
        title=f"{tpl['name']} - {emp['name']}", doc_no=params["doc_no"], purpose=params["purpose"],
        params=json.dumps(params, ensure_ascii=False), content=content, status="발급완료",
        signed_date=today().isoformat(),
    )
    msg = f"{params['doc_no']} 발급되었습니다"
    if cfg.pdf_on_signed:
        _, m = await run_in_threadpool(services.store_document_pdf, doc_id)
        msg += " · " + m
    return _redirect(f"/hr/documents/{doc_id}", msg)


# ════════════════════════════════════════════════════════════════════════
# 3. 연봉현황
# ════════════════════════════════════════════════════════════════════════
@router.get("/salaries", response_class=HTMLResponse)
def salaries(request: Request, year: int | None = None, department: str = ""):
    audit("hr", "연봉현황 열람", f"{year or today().year}년", department)
    years = repo.salary_years()
    year = year or today().year
    ov = services.salary_overview(year)
    if department:
        ov["rows"] = [r for r in ov["rows"] if r["employee"]["department"] == department]
    return _render(request, "hr/salaries.html", {
        "ov": ov, "years": years, "year": year, "trend": services.salary_trend(),
        "department": department, "depts": get_codes("DEPT"), "reasons": get_codes("SALARY_REASON"),
        "settings": get_settings(),
    }, "hr.salaries")


@router.post("/salaries")
async def salary_save(request: Request):
    f = dict(await request.form())
    amount = int(str(f["annual_salary"]).replace(",", ""))
    repo.upsert_salary(int(f["employee_id"]), int(f["year"]), amount, f.get("effective_date") or None,
                       f.get("reason") or "정기인상", f.get("note") or "")
    audit("hr", "연봉 저장", f"emp#{f['employee_id']}", f"{f['year']}년")  # 금액은 기록하지 않음
    return _redirect(f.get("next") or f"/hr/salaries?year={f['year']}", "연봉이 저장되었습니다")


@router.post("/salaries/{sal_id}/delete")
async def salary_delete(request: Request, sal_id: int):
    f = dict(await request.form())
    repo.delete_salary(sal_id)
    return _redirect(f.get("next") or "/hr/salaries", "삭제되었습니다")


@router.get("/api/salaries/bulk-preview")
def salary_bulk_preview(year: int, rate: float, round_unit: int = 10000, only_missing: bool = True,
                        department: str = ""):
    rows = services.bulk_raise_preview(year, rate, round_unit, only_missing, department)
    return {"rows": rows, "total_base": sum(r["base"] for r in rows), "total_new": sum(r["new"] for r in rows)}


@router.post("/salaries/bulk")
async def salary_bulk_apply(request: Request):
    b = await request.json()
    year = int(b["year"])
    for r in b["rows"]:
        repo.upsert_salary(int(r["employee_id"]), year, int(r["new"]), f"{year}-01-01",
                           b.get("reason") or "정기인상", f"일괄인상 {b.get('rate')}%")
    audit("hr", "연봉 일괄인상", str(year), f"{len(b['rows'])}명 {b.get('rate')}%")
    return {"ok": True, "message": f"{len(b['rows'])}명의 {year}년 연봉이 반영되었습니다"}


@router.get("/salaries/export.csv")
def salary_export(year: int):
    ov = services.salary_overview(year)
    out = [["사번", "성명", "부서", "직급", "고용형태", "전년연봉", f"{year}년 연봉", "인상액", "인상률(%)", "사유"]]
    for r in ov["rows"]:
        e = r["employee"]
        out.append([e["emp_no"], e["name"], e["department"], e["rank"], e["employment_type"],
                    r["prev"]["annual_salary"] if r["prev"] else "", r["cur"]["annual_salary"] if r["cur"] else "",
                    r["raise_amount"] if r["raise_amount"] is not None else "",
                    f"{r['raise_rate']:.2f}" if r["raise_rate"] is not None else "",
                    r["cur"]["reason"] if r["cur"] else ""])
    audit("hr", "개인정보 내보내기", "연봉현황 CSV", str(year))
    return _csv_response(out, f"연봉현황_{year}.csv")


# ════════════════════════════════════════════════════════════════════════
# 4. 상벌현황
# ════════════════════════════════════════════════════════════════════════
@router.get("/rewards", response_class=HTMLResponse)
def rewards(request: Request, kind: str = "", year: str = ""):
    rows = repo.list_rewards(kind, year or None)
    allr = repo.list_rewards()
    years = sorted({r["action_date"][:4] for r in allr}, reverse=True)
    by_year = {}
    for r in allr:
        y = r["action_date"][:4]
        by_year.setdefault(y, {"포상": 0, "징계": 0})
        by_year[y][r["kind"]] += 1
    by_cat = {}
    for r in rows:
        by_cat[(r["kind"], r["category"])] = by_cat.get((r["kind"], r["category"]), 0) + 1
    by_dept = {}
    for r in rows:
        by_dept.setdefault(r["department"] or "미지정", {"포상": 0, "징계": 0})
        by_dept[r["department"] or "미지정"][r["kind"]] += 1
    effective = [r for r in allr if r["kind"] == "징계" and (not r["expunge_date"] or r["expunge_date"] >= today().isoformat())]
    return _render(request, "hr/rewards.html", {
        "rows": rows, "years": years, "f": {"kind": kind, "year": year},
        "by_year": dict(sorted(by_year.items())), "by_cat": by_cat, "by_dept": by_dept,
        "effective": effective,
        "total_amount": sum(r["amount"] or 0 for r in rows if r["kind"] == "포상"),
        "emps": repo.list_employees(status=""), "reward_categories": _reward_categories(),
        "expunge_months": DISCIPLINE_EXPUNGE_MONTHS,
    }, "hr.rewards")


@router.post("/rewards/save")
async def reward_save(request: Request):
    f = dict(await request.form())
    if f.get("kind") == "징계" and not f.get("expunge_date") and f.get("action_date"):
        m = DISCIPLINE_EXPUNGE_MONTHS.get(f.get("category"), 12)
        f["expunge_date"] = add_months(to_date(f["action_date"]), m).isoformat()
    if (f.get("category") or "").startswith("기타") and (f.get("category_etc") or "").strip():
        f["category"] = f["category_etc"].strip()
    repo.save_reward(int(f.get("id") or 0) or None, f)
    audit("hr", "상벌 저장", f.get("employee_id", ""), f"{f.get('kind')}/{f.get('category')}")
    return _redirect(f.get("next") or "/hr/rewards", "저장되었습니다")


@router.post("/rewards/{rid}/delete")
async def reward_delete(request: Request, rid: int):
    f = dict(await request.form())
    repo.delete_reward(rid)
    return _redirect(f.get("next") or "/hr/rewards", "삭제되었습니다")


# ════════════════════════════════════════════════════════════════════════
# 5. 근로특이사항 (단축근무·육아휴직 등)
# ════════════════════════════════════════════════════════════════════════
@router.get("/specials", response_class=HTMLResponse)
def specials(request: Request, category: str = "", status: str = ""):
    rows = repo.list_specials(category, status)
    allr = repo.list_specials()
    summary = {}
    for r in allr:
        s = summary.setdefault(r["category"], {"진행중": 0, "예정": 0, "종료": 0})
        s[r["status"]] += 1
    t = today()
    timeline = [r for r in allr if r["status"] in ("진행중", "예정") or
                (to_date(r["actual_end"] or r["end_date"]) and to_date(r["actual_end"] or r["end_date"]) >= add_months(t, -3))]
    return _render(request, "hr/specials.html", {
        "rows": rows, "summary": summary, "f": {"category": category, "status": status},
        "work_specials": WORK_SPECIALS, "emps": repo.list_employees(status="재직(전체)"),
        "timeline": sorted(timeline, key=lambda r: r["start_date"]),
        "timeline_start": add_months(t.replace(day=1), -3).isoformat(),
        "timeline_end": add_months(t.replace(day=1), 13).isoformat(),
    }, "hr.specials")


@router.post("/specials/save")
async def special_save(request: Request):
    f = dict(await request.form())
    repo.save_special(int(f.get("id") or 0) or None, f)
    audit("hr", "근로특이사항 저장", f.get("employee_id", ""), f.get("category", ""))
    return _redirect(f.get("next") or "/hr/specials", "저장되었습니다")


@router.post("/specials/{sid}/delete")
async def special_delete(request: Request, sid: int):
    f = dict(await request.form())
    repo.delete_special(sid)
    return _redirect(f.get("next") or "/hr/specials", "삭제되었습니다")


@router.get("/api/employees/{emp_id}/contract-defaults")
def contract_defaults(emp_id: int, category: str):
    emp = _emp_or_404(emp_id)
    return JSONResponse({"params": services.default_contract_params(emp, category),
                         "suggest": services.suggest_template_category(emp)})
