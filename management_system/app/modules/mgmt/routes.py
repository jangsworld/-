"""경영관리 화면 (prefix /mgmt) + 회사 관리 만기 (prefix /ga/company).

권한(미들웨어): 인건비·인사평가·인센티브 = 관리자·인사담당, 교육 이수·법정의무 일정 = 총무 담당 이상.
"""
from urllib.parse import quote

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse

from app.common import templates, today
from app.db import audit, get_settings, set_setting
from app.modules.ga import company
from app.modules.hr import repo as hr_repo
from app.modules.hr.routes import _csv_response
from app.modules.mgmt import compliance, fin, incentive, labor, review, training
from app.modules.mgmt.common import depts, employees_in_year
from app.modules.mgmt.constants import (
    COMPANY_CATEGORIES, COMPANY_CYCLES, COMPANY_EXAMPLES, COMPANY_STATUSES, COMPLIANCE_CATEGORIES, COMPLIANCE_CYCLES,
    FIN_ITEMS, TRAINING_CATEGORIES, TRAINING_CYCLES, TRAINING_METHODS, TRAINING_TARGETS,
)

router = APIRouter(prefix="/mgmt")
company_router = APIRouter(prefix="/ga/company")


def _who(request: Request) -> str:
    u = request.state.user
    return u.get("display_name") or u.get("username") or ""


def _go(url: str, msg: str = "") -> RedirectResponse:
    if msg:
        url += ("&" if "?" in url else "?") + "msg=" + quote(msg)
    return RedirectResponse(url, status_code=303)


def _safe_next(v: str, default: str) -> str:
    v = str(v or "")
    return v if v.startswith("/") and not v.startswith("//") else default


def _render(request: Request, name: str, ctx: dict, active: str):
    ctx.setdefault("active_menu", active)
    ctx.setdefault("msg", request.query_params.get("msg", ""))
    return templates.TemplateResponse(request, name, ctx)


def _int(v, default=0) -> int:
    try:
        return int(str(v).replace(",", "").strip() or default)
    except (TypeError, ValueError):
        return default


def _year(v, default: int) -> int:
    y = _int(v, default)
    return y if 2000 <= y <= 2100 else default


@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
def mgmt_root():
    return RedirectResponse("/mgmt/labor")


# ── 손익 요약 (fund 읽기 / 직접 입력) ─────────────────────────────────────
@router.post("/fin/pull")
async def fin_pull(request: Request):
    f = await request.form()
    y = _year(f.get("year"), today().year)
    nxt = _safe_next(f.get("next"), "/mgmt/labor")
    try:
        d = await run_in_threadpool(fin.pull, y, _who(request))
    except fin.FundError as ex:
        return _go(nxt, str(ex))
    return _go(nxt, f"fund에서 {y}년 손익 요약을 읽었습니다 ({d.get('basis')}, {d.get('as_of')} 기준)")


@router.post("/fin/manual")
async def fin_manual(request: Request):
    f = await request.form()
    y = _year(f.get("year"), today().year)
    vals = {}
    for i, item in enumerate(FIN_ITEMS):
        v = str(f.get(f"v{i}") or "").replace(",", "").strip()
        if v:
            try:
                vals[item] = int(float(v))
            except ValueError:
                return _go(_safe_next(f.get("next"), "/mgmt/labor"), f"{item}은(는) 숫자로 넣어 주세요")
    fin.save_manual(y, vals, _who(request))
    return _go(_safe_next(f.get("next"), "/mgmt/labor"), f"{y}년 손익 요약을 저장했습니다")


# ── 인건비 총괄 ─────────────────────────────────────────────────────────
@router.get("/labor", response_class=HTMLResponse)
def labor_page(request: Request, year: int | None = None):
    years = sorted(set(hr_repo.salary_years()) | set(incentive.years()) | {today().year})
    y = year if year in years else today().year
    ov = labor.overview(y)
    return _render(request, "mgmt/labor.html", {
        "ov": ov, "year": y, "years": years, "trend": labor.trend([x for x in years if x <= today().year][-6:]),
        "fin_items": FIN_ITEMS, "fund_ready": fin.fund_configured(),
    }, "mgmt.labor")


@router.post("/labor/rates")
async def labor_rates(request: Request):
    f = await request.form()
    try:
        er, rr = float(f.get("employer") or 0), float(f.get("retire") or 0)
    except ValueError:
        return _go("/mgmt/labor", "비율은 숫자로 넣어 주세요")
    if not (0 <= er <= 30 and 0 <= rr <= 20):
        return _go("/mgmt/labor", "비율 범위를 확인하세요 (4대보험 0~30%, 퇴직 0~20%)")
    set_setting("labor_employer_rate", str(er))
    set_setting("labor_retire_rate", str(rr))
    audit("mgmt", "인건비 회사부담률 변경", "", f"{er}% / {rr}%")
    return _go(f"/mgmt/labor?year={_int(f.get('year'), today().year)}", "회사 부담률을 저장했습니다")


@router.get("/labor/export.csv")
def labor_export(year: int):
    ov = labor.overview(year)
    rows = [["부서", "성명", "직급", "근무개월", "연봉(연간)", "연봉(근무개월분)", "인센티브", "4대보험 회사부담", "퇴직급여 충당", "합계"]]
    for p in ov["people"]:
        rows.append([p["department"], p["name"], p["rank"] or "", p["months"], p["annual"] or "", p["salary"], p["incentive"],
                     p["employer"], p["retire"], p["total"]])
    audit("mgmt", "인건비 내려받기", str(year), "")
    return _csv_response(rows, f"인건비_{year}.csv")


# ── 인센티브 예산 ───────────────────────────────────────────────────────
@router.get("/incentive", response_class=HTMLResponse)
def incentive_page(request: Request, year: int | None = None, department: str = ""):
    years = sorted(set(incentive.years()) | {today().year})
    y = year if (year and 2000 <= year <= 2100) else today().year
    if y not in years:
        years = sorted(years + [y])
    ov = incentive.overview(y)
    emps = employees_in_year(y)
    return _render(request, "mgmt/incentive.html", {
        "ov": ov, "year": y, "years": years, "department": department, "depts": depts(),
        "pays": [p for p in ov["payments"] if not department or p["department"] == department],
        "emps": [{"id": e["id"], "name": e["name"], "department": e.get("department") or "", "rank": e.get("rank") or ""} for e in emps],
        "today_iso": today().isoformat(),
    }, "mgmt.incentive")


@router.post("/incentive/plan")
async def incentive_plan(request: Request):
    f = await request.form()
    y = _year(f.get("year"), today().year)
    budgets = {}
    for d, a in zip(f.getlist("dept"), f.getlist("amount")):
        d = str(d).strip()
        if d:
            budgets[d] = _int(a)
    try:
        incentive.save_plan(y, _int(f.get("total")), str(f.get("note") or "").strip(), budgets, _who(request))
    except incentive.IncentiveError as ex:
        return _go(f"/mgmt/incentive?year={y}", str(ex))
    return _go(f"/mgmt/incentive?year={y}", "예산을 저장했습니다")


@router.post("/incentive/pay")
async def incentive_pay(request: Request):
    f = await request.form()
    y = _year(f.get("year"), today().year)
    keys = ("pay_date", "department", "employee_id", "amount", "reason", "note")
    cols = {k: f.getlist(k) for k in keys}
    n = max(len(v) for v in cols.values())
    rows = [{k: (cols[k][i] if i < len(cols[k]) else "") for k in keys} for i in range(n)]
    for r in rows:      # 공통 지급일·사유 (일괄 지급)
        r["pay_date"] = r["pay_date"] or f.get("common_date") or ""
        r["reason"] = r["reason"] or f.get("common_reason") or ""
    try:
        cnt = incentive.add_payments(y, rows, _who(request))
    except incentive.IncentiveError as ex:
        return _go(f"/mgmt/incentive?year={y}", str(ex))
    return _go(f"/mgmt/incentive?year={y}", f"지급 {cnt}건을 등록했습니다")


@router.post("/incentive/pay/{pid}/delete")
async def incentive_pay_delete(request: Request, pid: int):
    f = await request.form()
    incentive.delete_payment(pid)
    return _go(f"/mgmt/incentive?year={_int(f.get('year'), today().year)}", "삭제했습니다")


@router.get("/incentive/export.csv")
def incentive_export(year: int):
    ov = incentive.overview(year)
    rows = [["부서", "배정 예산", "지급액", "잔액", "소진율(%)"]]
    rows += [[r["department"], r["budget"], r["paid"], r["remain"], round(r["rate"], 1) if r["rate"] is not None else ""] for r in ov["rows"]]
    rows += [[], ["지급일", "부서", "성명", "금액", "사유", "메모"]]
    rows += [[p["pay_date"], p["department"], p.get("name") or "(부서 지급)", p["amount"], p.get("reason") or "", p.get("note") or ""]
             for p in ov["payments"]]
    audit("mgmt", "인센티브 내려받기", str(year), "")
    return _csv_response(rows, f"인센티브_{year}.csv")


# ── 인사평가·연봉조정 ───────────────────────────────────────────────────
def _cycle(year: int) -> dict:
    c = review.get(year)
    if not c:
        raise HTTPException(404, "그 해 인사평가가 없습니다")
    return c


@router.get("/review", response_class=HTMLResponse)
def review_page(request: Request, year: int | None = None, department: str = ""):
    cs = review.cycles()
    y = year or (cs[0]["year"] if cs else today().year - 1)
    c = review.get(y)
    ctx = {"cycles": cs, "year": y, "c": c, "department": department, "depts": depts(), "fin_items": FIN_ITEMS,
           "fund_ready": fin.fund_configured()}
    if c:
        res = review.compute(c)
        ctx.update(res=res, sc=review.scenarios(c),
                   rows=[r for r in res["rows"] if not department or (r["employee"].get("department") or "") == department])
    else:
        ctx["fin"] = fin.summary(y)
    return _render(request, "mgmt/review.html", ctx, "mgmt.review")


@router.post("/review/create")
async def review_create(request: Request):
    f = await request.form()
    y = _year(f.get("year"), today().year - 1)
    review.create(y, _who(request))
    return _go(f"/mgmt/review?year={y}", f"{y}년 성과 인사평가를 만들었습니다")


@router.post("/review/{year}/setup")
async def review_setup(request: Request, year: int):
    c = _cycle(year)
    f = await request.form()
    try:
        review.save_setup(c, dict(f), _who(request))
    except review.ReviewError as ex:
        return _go(f"/mgmt/review?year={year}", str(ex))
    return _go(f"/mgmt/review?year={year}#scenario", "기준을 저장했습니다")


@router.post("/review/{year}/items")
async def review_items(request: Request, year: int):
    c = _cycle(year)
    f = await request.form()
    keys = ("employee_id", "grade", "add_rate", "final_rate", "comment", "evaluator")
    cols = {k: f.getlist(k) for k in keys}
    rows = [{k: (cols[k][i] if i < len(cols[k]) else "") for k in keys} for i in range(len(cols["employee_id"]))]
    dept = str(f.get("department") or "")
    try:
        n = review.save_items(c, rows, _who(request))
    except review.ReviewError as ex:
        return _go(f"/mgmt/review?year={year}&department={quote(dept)}#eval", str(ex))
    return _go(f"/mgmt/review?year={year}&department={quote(dept)}#eval", f"{n}명 평가를 저장했습니다")


@router.post("/review/{year}/status")
async def review_status(request: Request, year: int):
    c = _cycle(year)
    f = await request.form()
    try:
        review.set_status(c, str(f.get("status") or ""), _who(request))
    except review.ReviewError as ex:
        return _go(f"/mgmt/review?year={year}", str(ex))
    return _go(f"/mgmt/review?year={year}", f"'{f.get('status')}'(으)로 바꿨습니다")


@router.post("/review/{year}/apply")
async def review_apply(request: Request, year: int):
    c = _cycle(year)
    try:
        n = review.apply(c, _who(request))
    except review.ReviewError as ex:
        return _go(f"/mgmt/review?year={year}", str(ex))
    return _go(f"/mgmt/review?year={year}", f"{n}명의 {c['apply_year']}년 연봉을 연봉현황에 반영했습니다")


@router.get("/review/{year}/sheet.csv")
def review_sheet(year: int, department: str = ""):
    c = _cycle(year)
    audit("mgmt", "평가표 내려받기", str(year), department or "전체")
    return _csv_response(review.sheet_rows(c, department), f"인사평가표_{year}_{department or '전체'}.csv")


@router.post("/review/{year}/sheet")
async def review_sheet_upload(request: Request, year: int, file: UploadFile = File(...)):
    c = _cycle(year)
    raw = await file.read()
    for codec in ("utf-8-sig", "cp949"):
        try:
            text = raw.decode(codec)
            break
        except UnicodeDecodeError:
            continue
    else:
        return _go(f"/mgmt/review?year={year}#eval", "파일 글자를 읽지 못했습니다 (CSV로 저장해 주세요)")
    try:
        n, errs = review.import_sheet(c, text, _who(request))
    except review.ReviewError as ex:
        return _go(f"/mgmt/review?year={year}#eval", str(ex))
    msg = f"평가표에서 {n}명을 반영했습니다" + (f" · 확인 필요 {len(errs)}건: " + "; ".join(errs[:3]) if errs else "")
    return _go(f"/mgmt/review?year={year}#eval", msg)


@router.get("/review/{year}/result.csv")
def review_result(year: int):
    c = _cycle(year)
    res = review.compute(c)
    rows = [["부서", "사번", "성명", "직급", "등급", "평가 가산(%p)", "최종 인상률(%)", f"기준 연봉({c['year']})",
             f"새 연봉({c['apply_year']})", "인상액", "최저임금 미달"]]
    for r in res["rows"]:
        e = r["employee"]
        rows.append([e.get("department") or "", e.get("emp_no") or "", e["name"], e.get("rank") or "", r["grade"],
                     round(r["add_rate"], 2), round(r["final_rate"], 2), r["base_salary"] or "", r["new_salary"] or "",
                     r["raise"] or "", "예" if r["below_min"] else ""])
    audit("mgmt", "인사평가 결과 내려받기", str(year), "")
    return _csv_response(rows, f"연봉조정_{c['apply_year']}.csv")


# ── 교육 이수 ───────────────────────────────────────────────────────────
@router.get("/training", response_class=HTMLResponse)
def training_page(request: Request, year: int | None = None, department: str = "", tab: str = "matrix", course: int = 0):
    y = year if (year and 2000 <= year <= 2100) else today().year
    m = training.matrix(y, department)
    emps = employees_in_year(y)
    cs_all = training.courses(active_only=False)
    notdone = {}
    for ci, c in enumerate(m["courses"]):
        notdone[c["id"]] = [r["employee"]["id"] for r in m["rows"]
                            if r["cells"][ci] and r["cells"][ci]["status"] != "done"]
    return _render(request, "mgmt/training.html", {
        "mx": m, "notdone": notdone, "year": y, "department": department, "depts": depts(), "tab": tab,
        "records": training.records(y, course or None) if tab == "records" else [],
        "course_id": course, "courses_all": cs_all,
        "targets": {c["id"]: sorted(training.target_ids(c["id"])) for c in cs_all if c["target"] == "지정 직원"},
        "emps": [{"id": e["id"], "name": e["name"], "department": e.get("department") or "", "rank": e.get("rank") or ""} for e in emps],
        "cycles": TRAINING_CYCLES, "target_opts": TRAINING_TARGETS, "categories": TRAINING_CATEGORIES, "methods": TRAINING_METHODS,
        "today_iso": today().isoformat(),
    }, "mgmt.training")


@router.post("/training/records")
async def training_add(request: Request):
    f = await request.form()
    y = _year(f.get("year"), today().year)
    try:
        n = training.add_records(_int(f.get("course_id")), f.getlist("emp_ids"), str(f.get("edu_date") or ""),
                                 str(f.get("hours") or "0"), str(f.get("method") or ""), str(f.get("provider") or "").strip(),
                                 str(f.get("note") or "").strip(), _who(request))
    except training.TrainingError as ex:
        return _go(f"/mgmt/training?year={y}", str(ex))
    return _go(f"/mgmt/training?year={y}", f"{n}명 이수를 기록했습니다")


@router.post("/training/records/{rid}/delete")
async def training_delete(request: Request, rid: int):
    f = await request.form()
    training.delete_record(rid)
    return _go(f"/mgmt/training?year={_int(f.get('year'), today().year)}&tab=records", "삭제했습니다")


@router.post("/training/courses")
async def training_course_save(request: Request):
    f = await request.form()
    try:
        training.save_course(dict(f))
    except training.TrainingError as ex:
        return _go("/mgmt/training?tab=courses", str(ex))
    return _go("/mgmt/training?tab=courses", "과정을 저장했습니다")


@router.post("/training/courses/{cid}/targets")
async def training_targets(request: Request, cid: int):
    f = await request.form()
    if not training.course(cid):
        raise HTTPException(404)
    ids = f.getlist("emp_ids")
    training.set_targets(cid, ids)
    return _go("/mgmt/training?tab=courses", f"대상자 {len(ids)}명을 지정했습니다")


@router.get("/training/export.csv")
def training_export(year: int):
    m = training.matrix(year)
    label = {"done": "이수", "partial": "부족", "missing": "미이수", "upcoming": "예정"}
    rows = [["부서", "성명", "직급"] + [c["name"] for c in m["courses"]]]
    for r in m["rows"]:
        e = r["employee"]
        rows.append([e.get("department") or "", e["name"], e.get("rank") or ""] +
                    [label.get(x["status"], "") if x else "대상 아님" for x in r["cells"]])
    audit("mgmt", "교육 이수 현황 내려받기", str(year), "")
    return _csv_response(rows, f"교육이수_{year}.csv")


# ── 법정의무 일정 ───────────────────────────────────────────────────────
@router.get("/compliance", response_class=HTMLResponse)
def compliance_page(request: Request, year: int | None = None, src: str = "", show: str = "todo"):
    y = year if (year and 2000 <= year <= 2100) else today().year
    srcs = tuple(s for s in src.split(",") if s) or ("의무", "교육", "회사관리", "계약")
    ev = compliance.calendar_year(y, srcs)
    if show == "todo":
        shown = [e for e in ev if e["status"] not in ("done", "before")]
    else:
        shown = ev
    months = {}
    for e in shown:
        months.setdefault(e["date"].month, []).append(e)
    return _render(request, "mgmt/compliance.html", {
        "year": y, "events": ev, "months": months, "src": srcs, "show": show, "tasks": compliance.tasks(),
        "counts": {k: sum(1 for e in ev if e["status"] == k) for k in ("overdue", "soon", "upcoming", "done", "before")},
        "since": get_settings().get("compliance_since", ""),
        "cycles": COMPLIANCE_CYCLES, "categories": COMPLIANCE_CATEGORIES, "td": today(),
    }, "mgmt.compliance")


@router.post("/compliance/done")
async def compliance_done(request: Request):
    f = await request.form()
    y = _year(f.get("year"), today().year)
    try:
        if f.get("undo"):
            compliance.undo_done(_int(f.get("task_id")), str(f.get("period") or ""))
        else:
            compliance.mark_done(_int(f.get("task_id")), str(f.get("period") or ""), str(f.get("done_date") or ""),
                                 _who(request), str(f.get("note") or "").strip())
    except compliance.ComplianceError as ex:
        return _go(f"/mgmt/compliance?year={y}", str(ex))
    return _go(f"/mgmt/compliance?year={y}&show={quote(str(f.get('show') or 'todo'))}", "처리했습니다")


@router.post("/compliance/tasks")
async def compliance_task_save(request: Request):
    f = await request.form()
    try:
        compliance.save_task(dict(f))
    except compliance.ComplianceError as ex:
        return _go("/mgmt/compliance#tasks", str(ex))
    return _go("/mgmt/compliance?show=all#tasks", "일정을 저장했습니다")


@router.post("/compliance/tasks/{tid}/delete")
def compliance_task_delete(tid: int):
    compliance.delete_task(tid)
    return _go("/mgmt/compliance?show=all#tasks", "삭제했습니다")


# ── 회사 관리 만기 (/ga/company) ────────────────────────────────────────
@company_router.get("", response_class=HTMLResponse)
def company_page(request: Request, category: str = "", status: str = "관리중"):
    rows = company.items(category, status)
    return _render(request, "ga/company.html", {
        "rows": rows, "category": category, "status": status, "history": company.history(),
        "categories": COMPANY_CATEGORIES, "cycles": COMPANY_CYCLES, "statuses": COMPANY_STATUSES, "examples": COMPANY_EXAMPLES,
        "alerts": [r for r in rows if r["alert"]], "can_write": request.state.user["role"] in ("admin", "hr", "ga"),
        "next_expire": {r["id"]: (company.next_expire(r).isoformat() if company.next_expire(r) else "") for r in rows},
    }, "ga.company")


@company_router.post("/save")
async def company_save(request: Request):
    f = await request.form()
    try:
        company.save(dict(f), _who(request))
    except company.CompanyError as ex:
        return _go("/ga/company", str(ex))
    return _go("/ga/company", "저장했습니다")


@company_router.post("/{iid}/delete")
def company_delete(iid: int):
    company.delete(iid)
    return _go("/ga/company", "삭제했습니다")


@company_router.post("/{iid}/renew")
async def company_renew(request: Request, iid: int):
    f = await request.form()
    try:
        company.renew(iid, str(f.get("new_expire") or ""), str(f.get("done_date") or ""), str(f.get("note") or "").strip(),
                      _who(request))
    except company.CompanyError as ex:
        return _go("/ga/company", str(ex))
    return _go("/ga/company", "갱신했습니다")
