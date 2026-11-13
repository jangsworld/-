"""인사관리 페이지 및 API 라우트 (prefix: /hr)."""
import csv
import io
import json
import os
import re
from urllib.parse import quote

from fastapi import APIRouter, Request, HTTPException, UploadFile, File
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse, Response

from app.common import templates, today, sanitize_html, add_months, days_until, to_date
from app.db import batch, execute, get_codes, get_settings, audit
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
        "depts": repo.codes_with_used("DEPT"), "ranks": repo.codes_with_used("RANK"), "titles": repo.codes_with_used("TITLE"),
        "locations": repo.codes_with_used("LOCATION"), "employment_types": get_codes("EMPLOYMENT_TYPE"),
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
        notice = ecount_wait = None
    else:                                                  # 연차 촉진 기간 알림 (인사 담당자만)
        from app.modules.hr import leave
        try:
            notice = leave.stage_notice(today().year)
        except Exception:                                  # 알림 계산 실패가 대시보드를 막지 않도록
            notice = None
        from app.modules.hr import ecount
        try:
            ecount_wait = len(ecount.pending())
        except Exception:
            ecount_wait = None
    return _render(request, "hr/dashboard.html", {"d": d, "leave_notice": notice, "ecount_wait": ecount_wait},
                   "hr.dashboard")


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
    # 저장 작업은 작업 스레드에서: 여러 명을 저장하는 동안 다른 사람의 화면이 멈추지 않게
    return _redirect("/hr/employees", await run_in_threadpool(import_employees_csv, text, file.filename or ""))


CSV_DATE_FIELDS = ("birth_date", "hire_date", "probation_end", "resign_date")
CSV_DEFAULT_EMPLOYMENT = "정규직"          # 새 직원의 고용형태가 비어 있을 때 (등록 화면 기본값과 같음)


def import_employees_csv(text: str, filename: str = "") -> str:
    """CSV 일괄등록. 모든 줄을 먼저 확인하고, 하나라도 틀리면 아무것도 저장하지 않습니다(일부만 들어가는 일 없음).
    기존 직원(사번, 사번이 없으면 성명+입사일로 찾음)은 CSV 에 값이 있는 칸만 고칩니다. 빈 칸은 기존 값을 지우지 않습니다."""
    label_to_field = {k: f for f, k in CSV_COLUMNS}
    labels = dict(CSV_COLUMNS)
    try:
        rows = list(csv.DictReader(io.StringIO(text)))
    except csv.Error as ex:
        return f"CSV 파일을 읽을 수 없습니다 ({ex}). 아무것도 저장하지 않았습니다"
    if rows and not any(k and k.strip() in label_to_field for k in rows[0]):
        return "CSV 첫 줄(제목)이 양식과 다릅니다. [양식 받기]로 받은 파일에 입력해 주세요. 아무것도 저장하지 않았습니다"

    current = repo.list_employees(status="")
    by_no = {e["emp_no"]: e for e in current}
    by_name_hire: dict[tuple, list] = {}
    for e in current:
        by_name_hire.setdefault((e["name"], e["hire_date"]), []).append(e)

    plan, errors, seen_no, defaulted = [], [], {}, 0
    for line, row in enumerate(rows, start=2):                   # 1행은 제목
        data = {label_to_field[k.strip()]: (v or "").strip() for k, v in row.items()
                if k and k.strip() in label_to_field and isinstance(v, str)}
        if not any(data.values()):
            continue                                              # 빈 줄
        problems = []
        for dk in CSV_DATE_FIELDS:
            if data.get(dk):
                d = to_date(data[dk])
                if d:
                    data[dk] = d.isoformat()
                else:
                    problems.append(f"{labels[dk]} '{data[dk]}' 날짜 형식 오류 (예: 2026-03-02)")
        if not data.get("name"):
            problems.append("성명 없음")
        existing = by_no.get(data.get("emp_no")) if data.get("emp_no") else None
        if not existing and not data.get("emp_no") and data.get("name") and data.get("hire_date"):
            same = by_name_hire.get((data["name"], data["hire_date"]), [])
            existing = same[0] if len(same) == 1 else None        # 사번 없이 다시 올려도 같은 사람이 두 번 생기지 않게
        if not existing and not data.get("hire_date"):
            problems.append("입사일 없음 (새 직원은 필수)")
        if data.get("emp_no"):
            if data["emp_no"] in seen_no:
                problems.append(f"사번 {data['emp_no']}이(가) {seen_no[data['emp_no']]}행과 겹침")
            seen_no.setdefault(data["emp_no"], line)
        if problems:
            errors.append(f"{line}행({data.get('name') or '이름 없음'}): " + ", ".join(problems))
            continue
        if existing:
            changes = {k: v for k, v in data.items() if v != ""}
            edu = ("edu_level", "edu_school", "edu_major", "edu_grad_year")
            if any(k in changes for k in edu):                    # 학력 요약이 일부 칸만으로 다시 만들어지지 않게
                changes = {**{k: existing.get(k) for k in edu}, **changes}
            plan.append(("update", existing["id"], changes))
        else:
            if not data.get("employment_type"):
                data["employment_type"] = CSV_DEFAULT_EMPLOYMENT
                defaulted += 1
            plan.append(("create", None, data))

    if errors:
        more = f" 외 {len(errors) - 5}건" if len(errors) > 5 else ""
        return f"일괄등록을 하지 않았습니다. 고칠 곳 {len(errors)}건: " + " / ".join(errors[:5]) + more + \
               " — 고쳐서 다시 올려 주세요 (아무것도 바뀌지 않았습니다)"
    if not plan:
        return "등록할 내용이 없습니다 (빈 파일)"
    with batch():                                                 # 한 번에 저장: 중간 오류 시 전부 취소
        for kind, emp_id, data in plan:
            if kind == "update":
                repo.update_employee(emp_id, data)
            else:
                repo.create_employee(data)
        added = sum(1 for p in plan if p[0] == "create")
        new_codes = {g: repo.add_missing_codes(g, [p[2].get(f) for p in plan])     # 목록에 없던 부서·직급 등은 구분값에 추가
                     for f, g in repo.EMPLOYEE_CODE_FIELDS.items()}
        audit("hr", "임직원 일괄등록", filename, f"신규 {added} / 수정 {len(plan) - added}")
    note = f" (고용형태가 비어 있던 {defaulted}명은 '{CSV_DEFAULT_EMPLOYMENT}'으로 등록)" if defaulted else ""
    group_names = {"DEPT": "부서", "RANK": "직급", "TITLE": "직책", "LOCATION": "근무지", "EMPLOYMENT_TYPE": "고용형태",
                   "EDU_LEVEL": "학력", "EMERGENCY_RELATION": "비상연락처 관계"}
    codes_note = ", ".join(f"{group_names[g]} {', '.join(v)}" for g, v in new_codes.items() if v)
    if codes_note:
        note += f" · 구분값에 새로 추가: {codes_note}"
    return f"일괄등록 완료: 신규 {added}명 / 수정 {len(plan) - added}명{note}"


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
    if raw.get("paper_contract") == "1":                  # 기존 직원: 서면 계약 체결 완료로 바로 기록
        record_paper_contract(repo.get_employee(emp_id), raw.get("hire_date"), raw.get("paper_contract_end"))
        return _redirect(f"/hr/employees/{emp_id}", "임직원이 등록되었습니다 (근로계약: 기존 서면계약 체결)")
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
    d = repo.get_draft(draft_id)
    if d and d["status"] == "등록완료" and d["employee_id"]:     # 오래된 화면·링크: 오류 대신 등록된 직원으로 안내
        return _redirect(f"/hr/employees/{d['employee_id']}", "이미 임직원으로 등록된 초안입니다")
    if not d or d["status"] != "확인필요":
        return _redirect("/hr/employees", "이미 처리되었거나 삭제된 초안입니다")
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
    if raw.get("paper_contract") == "1":
        record_paper_contract(repo.get_employee(emp_id), raw.get("hire_date"), raw.get("paper_contract_end"))
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


@router.post("/drafts/{draft_id}/delete")
def draft_delete(draft_id: int):
    """초안 삭제: 읽어 둔 내용(개인정보 포함)을 지웁니다. 폴더·서류는 그대로이며,
    서류가 바뀌면(추가·교체) 다시 초안을 만듭니다. 같은 서류로는 다시 만들지 않습니다."""
    d = repo.get_draft(draft_id)
    if not d or d["status"] != "확인필요":
        return _redirect("/hr/employees", "이미 처리된 초안입니다")
    repo.update_draft(draft_id, status="삭제", data={})
    audit("hr", "등록초안 삭제", d["folder_name"], "")
    return _redirect("/hr/employees", f"{d['folder_name']} 초안을 삭제했습니다. 폴더에 서류를 새로 넣거나 바꾸면 다시 만들어집니다")


@router.get("/employees/{emp_id}", response_class=HTMLResponse)
def employee_detail(request: Request, emp_id: int, tab: str = "info"):
    e = _emp_or_404(emp_id)
    e["has_contract"] = emp_id in repo.valid_contract_ids()
    if request.state.user["role"] not in ("admin", "hr") and tab in ("salary", "contract", "reward", "special", "insurance", "payroll", "files", "check"):
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
        "insurances": repo.list_insurances(emp_id),
        **(_payroll_ctx(emp_id) if request.state.user["role"] in ("admin", "hr") else {}),
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


def _payroll_ctx(emp_id: int) -> dict:
    """인사기록카드 '급여·연말정산' 탭과 이카운트 반영 상태."""
    from app.modules.hr import ecount
    sync = {r["action"]: r for r in repo.query("SELECT * FROM ecount_sync WHERE employee_id=?", (emp_id,))}
    return {"payroll": ecount.get_payroll(emp_id), "pay_item_names": ecount.pay_items(),
            "contract_pay": ecount.contract_pay(emp_id), "insured_suggest": ecount.suggest_insured(emp_id),
            "ecount_sync": sync}


@router.get("/employees/{emp_id}/edit", response_class=HTMLResponse)
def employee_edit(request: Request, emp_id: int):
    e = _emp_or_404(emp_id)
    e["rrn_masked"] = repo.rrn_masked(emp_id)
    year = today().year
    sals = repo.employee_salaries(emp_id)
    ins = repo.list_insurances(emp_id)
    return _render(request, "hr/employee_form.html", {
        "e": e, "is_new": False, "quals": repo.get_qualifications(emp_id), **_codes(),
        # 한 화면 수정: 올해 연봉·급여·연말정산·상해공제·체크리스트도 같은 저장 버튼으로
        "sal_year": year, "sal_cur": next((x for x in sals if x["year"] == year), None), "sal_hist": sals[:5],
        "salary_reasons": get_codes("SALARY_REASON"),
        "ins_cur": max(ins, key=lambda x: (x.get("end_date") or "", x["id"])) if ins else None, "ins_count": len(ins),
        "checklists": {"ONBOARD": ("입사", get_codes("ONBOARD")), "OFFBOARD": ("퇴사", get_codes("OFFBOARD"))},
        "checks": repo.list_checks(emp_id), **_payroll_ctx(emp_id),
    }, "hr.employees")


def _save_more(emp_id: int, raw: dict, by: str):
    """수정 화면의 추가 구역(연봉·급여·연말정산·상해공제·체크리스트) 저장. 형식 오류는 ValueError."""
    from app.modules.hr import ecount
    num = lambda v: str(v or "").replace(",", "").strip()
    # 연봉 (그 해 한 줄)
    amt = num(raw.get("sal_amount"))
    if amt:
        if not amt.isdigit() or int(amt) <= 0:
            raise ValueError("연봉은 0보다 큰 숫자로 넣어 주세요")
        y = num(raw.get("sal_year"))
        if not (y.isdigit() and 2000 <= int(y) <= 2100):
            raise ValueError("연봉 적용연도를 확인해 주세요")
        cur = next((x for x in repo.employee_salaries(emp_id) if x["year"] == int(y)), None)
        eff = raw.get("sal_date") or None
        if eff and not to_date(eff):
            raise ValueError("연봉 적용일 형식이 맞지 않습니다")
        if not cur or int(cur["annual_salary"]) != int(amt) or (eff and eff != cur.get("effective_date")) \
                or (raw.get("sal_reason") or cur.get("reason")) != cur.get("reason"):
            repo.upsert_salary(emp_id, int(y), int(amt), eff, raw.get("sal_reason") or "정기인상",
                               (raw.get("sal_note") or (cur or {}).get("note") or ""))
            audit("hr", "연봉 저장", f"emp#{emp_id}", f"{y}년 (인사기록 수정)")
    # 급여·연말정산
    if "insured_monthly" in raw:
        def iv(k, lo, hi):
            v = num(raw.get(k))
            if not v:
                return None
            if not v.isdigit() or not lo <= int(v) <= hi:
                raise ValueError("급여·연말정산 숫자 칸에는 0 이상의 숫자만 적어 주세요")
            return int(v)
        data = {"householder": raw.get("householder") if raw.get("householder") in ("Y", "N") else None,
                "dependents": iv("dependents", 0, 30), "children_8_20": iv("children_8_20", 0, 20),
                "insured_monthly": iv("insured_monthly", 0, 10**9), "note": (raw.get("pay_note") or "").strip()[:500],
                "pay_items": {}}
        for name in ecount.pay_items():
            v = iv(f"pay_{name}", 0, 10**9)
            if v:
                data["pay_items"][name] = v
        if data["children_8_20"] is not None and data["dependents"] is not None and data["children_8_20"] > data["dependents"]:
            raise ValueError("8~20세 자녀 수가 부양가족 수보다 많습니다")
        old = ecount.get_payroll(emp_id)
        keys = ("householder", "dependents", "children_8_20", "insured_monthly", "note", "pay_items")
        if old.get("saved") or any(data[k] for k in keys):
            if not old.get("saved") or any((old.get(k) or None) != (data[k] or None) for k in keys):
                ecount.save_payroll(emp_id, data)
    # 상해공제 (가장 최근 기록 수정 또는 새로 등록)
    prod = (raw.get("ins_product") or "").strip()
    if prod:
        d = {k: (raw.get("ins_" + k) or "").strip() for k in ("insurer", "joined_on", "start_date", "end_date", "policy_no", "premium", "note")}
        for k in ("joined_on", "start_date", "end_date"):
            if d[k] and not to_date(d[k]):
                raise ValueError("상해공제 날짜 형식이 맞지 않습니다")
        if d["start_date"] and d["end_date"] and d["end_date"] < d["start_date"]:
            raise ValueError("상해공제 보장 종료일이 시작일보다 빠릅니다")
        d["premium"] = re.sub(r"[^\d]", "", d["premium"])
        d.update(product=prod, employee_id=emp_id)
        ins_id = int(raw["ins_id"]) if str(raw.get("ins_id") or "").isdigit() else None
        if ins_id and not repo.query_one("SELECT 1 x FROM employee_insurances WHERE id=? AND employee_id=?", (ins_id, emp_id)):
            raise ValueError("상해공제 기록을 찾을 수 없습니다")
        old = repo.query_one("SELECT * FROM employee_insurances WHERE id=?", (ins_id,)) if ins_id else None
        cols = ("product", "insurer", "joined_on", "start_date", "end_date", "policy_no", "premium", "note")
        if not old or any(str(old.get(c) or "") != str(d.get(c) or "") for c in cols):
            repo.save_insurance(d, ins_id)
    # 체크리스트
    if raw.get("checks_shown"):
        have = repo.list_checks(emp_id)
        for kind in ("ONBOARD", "OFFBOARD"):
            for i, item in enumerate(get_codes(kind)):
                want = bool(raw.get(f"chk_{kind}_{i}"))
                if want != ((kind, item) in have) and (kind == "ONBOARD" or raw.get(f"chk_{kind}_shown")):
                    repo.set_check(emp_id, kind, item, want, by)


@router.post("/employees/{emp_id}/edit")
async def employee_update(request: Request, emp_id: int):
    _emp_or_404(emp_id)
    raw = dict(await request.form())
    appt_date = raw.pop("appt_date", None) or None
    back_tab = raw.pop("back_tab", "") or "info"
    try:
        with batch():                     # 한 번 저장: 하나라도 틀리면 아무것도 바뀌지 않음
            form, rrn, quals = _employee_form(raw)
            repo.update_employee(emp_id, form, appt_date)
            _save_extras(emp_id, rrn, quals)
            _save_more(emp_id, raw, request.state.user["display_name"])
    except ValueError as ex:
        return _redirect(f"/hr/employees/{emp_id}/edit", str(ex))
    except Exception as ex:
        return _redirect(f"/hr/employees/{emp_id}/edit", f"저장 실패: {ex}")
    return _redirect(f"/hr/employees/{emp_id}?tab={back_tab}", "저장되었습니다")


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
    n = await run_in_threadpool(_contract_bulk, ids)
    return _redirect("/hr/contracts?status=작성중", f"{n}건의 근로계약서 초안이 생성되었습니다")


PAPER_CONTRACT_HTML = ("<p>BMS 도입 전에 서면(종이·외부 전자계약)으로 체결한 근로계약입니다.</p>"
                       "<p>계약서 원본은 임직원 서류 폴더(인사기록카드의 입사서류 탭)에서 확인하세요.</p>")


def record_paper_contract(emp: dict, start_date: str | None = None, end_date: str | None = None) -> int | None:
    """기존 서면 계약을 '체결완료' 근로계약으로 기록 (계약서 본문은 만들지 않음). 이미 유효한 계약이 있으면 건너뜀."""
    if emp["id"] in repo.valid_contract_ids():
        return None
    start = (to_date(start_date) or to_date(emp.get("hire_date")) or today()).isoformat()
    end = to_date(end_date).isoformat() if to_date(end_date) else None
    doc_id = repo.create_document(
        doc_group="contract", employee_id=emp["id"], template_id=None, category=services.suggest_template_category(emp),
        title=f"근로계약서(기존 서면) - {emp['name']}", start_date=start, end_date=end,
        content=PAPER_CONTRACT_HTML, status="체결완료", signed_date=start,
        params=json.dumps({"source": "기존 서면계약"}, ensure_ascii=False))
    audit("hr", "기존 서면계약 체결 처리", emp["name"], f"{start}~{end or ''}")
    return doc_id


@router.post("/contracts/paper")
async def contract_paper(request: Request):
    """미체결 재직자 중 선택한 인원을 '기존 서면계약 체결'로 한 번에 처리 (입사일부터, 기간 정함 없음)."""
    form = await request.form()
    ids = [int(x) for x in form.getlist("employee_ids")]

    def apply() -> int:
        n = 0
        with batch():
            for emp_id in ids:
                emp = repo.get_employee(emp_id)
                if emp and record_paper_contract(emp):
                    n += 1
        return n

    n = await run_in_threadpool(apply)
    return _redirect("/hr/contracts", f"{n}명을 기존 서면계약 체결로 처리했습니다. 기간제 등 종료일이 있는 계약은 목록에서 열어 종료일을 넣어 주세요")


@router.post("/employees/{emp_id}/paper-contract")
async def employee_paper_contract(request: Request, emp_id: int):
    emp = _emp_or_404(emp_id)
    f = dict(await request.form())
    doc_id = record_paper_contract(emp, f.get("start_date"), f.get("end_date"))
    msg = "기존 서면계약 체결로 처리했습니다" if doc_id else "이미 유효한 근로계약이 있습니다"
    return _redirect(f"/hr/employees/{emp_id}?tab=contract", msg)


def _contract_bulk(ids: list[int]) -> int:
    tpls = {t["category"]: t for t in repo.list_templates("contract", active_only=True)}
    n = 0
    with batch():
        for emp_id in ids:
            emp = repo.get_employee(emp_id)
            if not emp:
                continue
            cat = services.suggest_template_category(emp)
            tpl = tpls.get(cat) or tpls.get("정규직")
            if not tpl:
                continue
            _create_contract(emp, tpl, services.default_contract_params(emp, cat))
            n += 1
    return n


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
        "mails": [{**m, "to_masked": _mask_email(m["to_addr"])} for m in repo.list_doc_mails(doc_id)],
        "history": repo.list_documents(d["doc_group"], employee_id=d["employee_id"]),
    }, {"contract": "hr.contracts", "leave": "hr.leave"}.get(d["doc_group"], "hr.certificates"))


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


_EMAIL_RE = re.compile(r"^[^@\s,;<>\"']+@[^@\s,;<>\"']+\.[^@\s,;<>\"']{2,}$")


def valid_email(v: str) -> bool:
    return bool(_EMAIL_RE.match(v or ""))


def email_document(doc_id: int, target: str = "employee", manual_to: str = "", note: str = "") -> tuple[bool, str]:
    """발급 문서를 PDF 첨부로 메일 전송. target: employee(인사기록카드 이메일) / manual(직접 입력)."""
    from app import storage
    from app.db import current_actor
    d = repo.get_document(doc_id)
    if not d:
        return False, "문서를 찾을 수 없습니다"
    if not mailer.configured():
        return False, "메일 서버가 설정되지 않았습니다 (회사정보·환경설정 → 메일)"
    if target == "manual":
        to, label = (manual_to or "").strip(), "직접입력"
        if not valid_email(to):
            return False, "받는 사람 메일 주소를 올바르게 입력하세요 (예: name@example.com, 한 명만)"
    else:
        to, label = (d.get("email") or "").strip(), "인사기록카드"
        if not valid_email(to):
            return False, "인사기록카드에 이메일이 없거나 형식이 맞지 않습니다. [직접 입력]으로 보내거나 인사기록카드를 고쳐 주세요"
    if d["status"] in ("발급취소", "해지", "만료"):
        return False, f"{d['status']} 상태의 문서는 보낼 수 없습니다"
    if "ph-missing" in (d["content"] or ""):
        return False, "비어 있는 항목([항목] 표시)이 있습니다. 문서를 완성·저장한 뒤 보내 주세요"
    if not d.get("pdf_path") or (d.get("pdf_saved_at") or "") < (d.get("updated_at") or ""):
        ok, m = services.store_document_pdf(doc_id)       # 고친 뒤 PDF 를 다시 만들어 최신본을 보냄
        if not ok:
            return False, m
        d = repo.get_document(doc_id)
    company = get_settings().get("company_name", "")
    note = (note or "").strip()[:1000]
    body = f"{d['name']}님, 요청하신 {d['category']}를 첨부해 보내드립니다."
    if d.get("doc_no"):
        body += f"\n발급번호: {d['doc_no']}"
    if note:
        body += f"\n\n{note}"
    body += f"\n\n{company} 경영관리팀"
    try:
        pdf = storage.read_verified(d["pdf_path"], d["pdf_sha256"])
        mailer.send(to, f"[{company}] {d['title']}", body, [(f"{d['category']}_{d['name']}.pdf", pdf)])
    except (storage.StorageError, mailer.MailError) as ex:
        audit("hr", "메일 전송 실패", f"doc#{doc_id}", str(ex)[:120])
        return False, str(ex)
    with batch():
        execute("UPDATE hr_documents SET emailed_at=? WHERE id=?", (signing._now(), doc_id))  # 수정일은 그대로
        repo.add_doc_mail(doc_id, label, to, current_actor.get()[1])
        audit("hr", "문서 메일 전송", f"doc#{doc_id}", f"{d['title']} · {label}")   # 주소 원문은 기록하지 않음
    return True, f"{_mask_email(to)} 로 전송했습니다"


def _mask_email(v: str) -> str:
    local, _, domain = (v or "").partition("@")
    return (local[:2] + "***@" + domain) if domain else v


@router.post("/documents/{doc_id}/email")
async def document_email(doc_id: int, request: Request):
    """발급한 증명서 등을 PDF 첨부로 메일 전송 (인사기록카드 이메일 또는 직접 입력한 주소)."""
    f = await request.form()
    ok, msg = await run_in_threadpool(email_document, doc_id, f.get("target") or "employee",
                                      f.get("to") or "", f.get("note") or "")
    return _redirect(f"/hr/documents/{doc_id}", msg)


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


@router.post("/templates/import")
async def template_import(file: UploadFile = File(...)):
    """회사 양식 PDF(또는 스캔 이미지) → 템플릿 초안(미사용). 올린 파일은 저장하지 않습니다."""
    from app.modules.hr import template_import as ti
    name = os.path.basename(file.filename or "")
    ext = os.path.splitext(name)[1].lower()
    if ext not in ti.SUPPORTED:
        return _redirect("/hr/templates", "한글(.hwp·.hwpx), PDF 또는 스캔 이미지(jpg·png) 파일을 올려 주세요")
    raw = await file.read(ti.MAX_UPLOAD_MB * 1024 * 1024 + 1)
    if len(raw) > ti.MAX_UPLOAD_MB * 1024 * 1024:
        return _redirect("/hr/templates", f"파일이 너무 큽니다 (최대 {ti.MAX_UPLOAD_MB}MB)")

    def work():
        import tempfile
        from app.modules.hr import hwp_reader
        fd, path = tempfile.mkstemp(suffix=ext)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw)
            if ext in ti.HWP_EXT:
                blocks = hwp_reader.read(path, name)
            else:
                lines, ocr = ti.read_lines(path, name)
        except hwp_reader.HwpError as ex:
            return None, str(ex)
        except Exception as ex:          # 손상된 파일 등: 오류 화면 대신 안내
            return None, f"파일을 읽지 못했습니다 ({ex.__class__.__name__}). 다른 파일로 시도해 주세요"
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        if ext in ti.HWP_EXT:                    # 한글 원본: 양식마다 템플릿 하나 (정렬·표 유지)
            results = ti.convert_hwp(blocks, get_settings())
            if not results:
                return None, "한글 파일에서 본문을 찾지 못했습니다"
            ids = []
            with batch():
                for res in results:
                    ids.append(repo.save_template(None, res["doc_group"], res["category"], res["name"],
                                                  sanitize_html(res["html"]), 0))
                audit("hr", "템플릿 한글 가져오기", name, f"{len(results)}개")
            if len(ids) == 1:
                return ids[0], ti.summary(results[0], False, "한글 문서")
            return -1, (f"한글 문서에서 양식 {len(ids)}개를 찾아 템플릿 {len(ids)}개를 만들었습니다 (모두 미사용 상태): "
                        + ", ".join(r["name"] for r in results) + ". 필요한 것만 열어 확인한 뒤 [사용]을 체크하세요")
        if not lines:
            return None, "파일에서 글자를 읽지 못했습니다. 스캔이 흐리면 더 선명한 PDF로 시도해 주세요"
        result = ti.convert(lines, get_settings())
        tid = repo.save_template(None, result["doc_group"], result["category"],
                                 result["name"] or os.path.splitext(name)[0], sanitize_html(result["html"]), 0)
        audit("hr", "템플릿 PDF 가져오기", result["name"], f"치환 {sum(result['changes'].values())}곳")
        return tid, ti.summary(result, ocr)

    tid, msg = await run_in_threadpool(work)
    return _redirect(f"/hr/templates/{tid}" if tid and tid > 0 else "/hr/templates", msg)


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
        "mail_ready": mailer.configured(),
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
    mail_mode = f.get("mail_mode") or "none"
    if cfg.pdf_on_signed or mail_mode in ("employee", "manual"):
        _, m = await run_in_threadpool(services.store_document_pdf, doc_id)
        msg += " · " + m
    if mail_mode in ("employee", "manual"):
        ok, m = await run_in_threadpool(email_document, doc_id, mail_mode, f.get("mail_to") or "", "")
        msg += " · " + ("메일 " + m if ok else "메일은 보내지 못했습니다: " + m)
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
        "department": department, "depts": repo.codes_with_used("DEPT"), "reasons": get_codes("SALARY_REASON"),
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

    def apply():
        with batch():                                   # 전원 한 번에 반영 (중간 오류 시 아무도 바뀌지 않음)
            for r in b["rows"]:
                repo.upsert_salary(int(r["employee_id"]), year, int(r["new"]), f"{year}-01-01",
                                   b.get("reason") or "정기인상", f"일괄인상 {b.get('rate')}%")
            audit("hr", "연봉 일괄인상", str(year), f"{len(b['rows'])}명 {b.get('rate')}%")

    await run_in_threadpool(apply)
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


@router.post("/employees/{emp_id}/insurances")
async def insurance_save(request: Request, emp_id: int):
    """상해공제 가입 등록·수정 (ins_id 가 있으면 수정)."""
    _emp_or_404(emp_id)
    f = dict(await request.form())
    back = f"/hr/employees/{emp_id}?tab=insurance"
    data = {k: (f.get(k) or "").strip() for k in ("product", "insurer", "joined_on", "start_date", "end_date",
                                                     "policy_no", "premium", "note")}
    data["employee_id"] = emp_id
    if not data["product"]:
        return _redirect(back, "공제명을 입력하세요")
    for k in ("joined_on", "start_date", "end_date"):
        if data[k] and not to_date(data[k]):
            return _redirect(back, "날짜 형식이 맞지 않습니다")
    if data["start_date"] and data["end_date"] and data["end_date"] < data["start_date"]:
        return _redirect(back, "보장 종료일이 시작일보다 빠릅니다")
    data["premium"] = re.sub(r"[^\d]", "", data["premium"])
    ins_id = int(f["ins_id"]) if str(f.get("ins_id") or "").isdigit() else None
    if ins_id and not repo.query_one("SELECT 1 x FROM employee_insurances WHERE id=? AND employee_id=?", (ins_id, emp_id)):
        return _redirect(back, "기록을 찾을 수 없습니다")
    repo.save_insurance(data, ins_id)
    return _redirect(back, "상해공제 정보를 저장했습니다")


@router.post("/employees/{emp_id}/insurances/{ins_id}/delete")
def insurance_delete(emp_id: int, ins_id: int):
    if repo.query_one("SELECT 1 x FROM employee_insurances WHERE id=? AND employee_id=?", (ins_id, emp_id)):
        repo.delete_insurance(ins_id)
    return _redirect(f"/hr/employees/{emp_id}?tab=insurance", "삭제되었습니다")


@router.get("/api/employees/{emp_id}/contract-defaults")
def contract_defaults(emp_id: int, category: str):
    emp = _emp_or_404(emp_id)
    return JSONResponse({"params": services.default_contract_params(emp, category),
                         "suggest": services.suggest_template_category(emp)})
