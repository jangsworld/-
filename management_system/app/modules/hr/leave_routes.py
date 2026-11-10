"""연차 관리 화면 (/hr/leave) 과 직원 사용계획 회신 화면 (/leave-plan/, 로그인 없이 링크+생년월일)."""
import html
import os
import re
import secrets
import tempfile
from datetime import datetime, timedelta
from urllib.parse import quote

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse

from app import mailer
from app.common import templates, to_date, today
from app.config import settings
from app.db import audit, batch, current_actor, execute, get_settings, query, query_one
from app.modules.hr import leave, repo, services
from app.modules.hr.signing import _hash, _now, base_url
from app.security.crypto import enc

router = APIRouter(prefix="/hr/leave")
public_router = APIRouter(prefix="/leave-plan")

COOKIE = "bms_leave"
MAX_FAILS = 5
SESSION_MINUTES = 30
REPLY_DAYS = 10            # 근로기준법 제61조: 촉진 통보를 받은 날부터 10일 이내 사용 시기 통보
MAX_PLAN_ROWS = 40


def _redirect(url: str, msg: str = "") -> RedirectResponse:
    if msg:
        url += ("&" if "?" in url else "?") + "msg=" + quote(msg)
    return RedirectResponse(url, status_code=303)


def _render(request: Request, name: str, ctx: dict):
    ctx.setdefault("active_menu", "hr.leave")
    ctx.setdefault("msg", request.query_params.get("msg", ""))
    return templates.TemplateResponse(request, name, ctx)


def _year(v) -> int:
    try:
        y = int(v)
        return y if 2000 <= y <= 2100 else today().year
    except (TypeError, ValueError):
        return today().year


def _fmt(n) -> str:
    return f"{n:g}"


# ════════════════════════════════════════════════════════════════════════
# 담당자 화면
# ════════════════════════════════════════════════════════════════════════
@router.get("", response_class=HTMLResponse)
def leave_home(request: Request, year: int | None = None, department: str = ""):
    year = _year(year)
    rows = leave.overview(year, department)
    audit("hr", "연차현황 열람", f"{year}년", department)
    months = leave.covered_months(year)
    sch = leave.schedule(year)
    t = today()
    stage = ("1차 촉진 기간" if sch["first_from"] <= t <= sch["first_to"] else
             "2차 지정 기간" if sch["first_to"] < t <= sch["second_by"] else "")
    depts = [r["department"] for r in query("SELECT DISTINCT department FROM employees WHERE department<>'' ORDER BY department")]
    return _render(request, "hr/leave.html", {
        "year": year, "rows": rows, "department": department, "depts": depts, "months": months,
        "last_month": (t.month if year == t.year else 12), "sch": sch, "stage": stage,
        "unmatched": leave.unmatched(), "imports": query("SELECT * FROM leave_imports ORDER BY id DESC LIMIT 8"),
        "mail_ready": mailer.configured(), "years": list(range(t.year + 1, t.year - 4, -1)),
        "employees": query("SELECT id, name, emp_no, department, resign_date FROM employees ORDER BY name"),
        "totals": {"granted": sum(r["granted"] for r in rows), "used": sum(r["used"] for r in rows),
                   "remaining": sum(r["remaining"] for r in rows)},
    })


@router.post("/import")
async def leave_import(files: list[UploadFile] = File(...)):
    items = []
    for f in files:
        name = os.path.basename(f.filename or "")
        raw = await f.read(10 * 1024 * 1024 + 1)
        if not name:
            continue
        if len(raw) > 10 * 1024 * 1024:
            items.append((name, None, "파일이 너무 큽니다 (최대 10MB)"))
            continue
        items.append((name, raw, ""))
    if not items:
        return _redirect("/hr/leave", "올릴 파일을 고르세요")

    def work():
        msgs = []
        for name, raw, err in sorted(items, key=lambda x: x[0]):      # 이름(기간) 순서대로 반영
            if err:
                msgs.append(f"{name}: {err}")
                continue
            ext = os.path.splitext(name)[1].lower()
            fd, path = tempfile.mkstemp(suffix=ext)
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(raw)
                s = leave.import_file(path, name)
                msg = f"{s['period']}: 추가 {s['added']} · 변경 {s['changed']} · 취소반영 {s['removed']}"
                if s["skipped"]:
                    msg += f" · 연차 외 {s['skipped']}건 제외"
                msgs.append(msg)
            except leave.LeaveImportError as ex:
                msgs.append(f"{name}: {ex}")
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass
        n = len(leave.unmatched())
        if n:
            msgs.append(f"직원 연결이 필요한 이름 {n}개 (아래 '이름 연결'에서 한 번만 연결하세요)")
        return " / ".join(msgs)

    return _redirect("/hr/leave", await run_in_threadpool(work))


@router.post("/link")
async def leave_link(request: Request):
    f = await request.form()
    name, emp = (f.get("mp_name") or "").strip(), f.get("employee_id")
    if not name or not (emp or "").isdigit() or not repo.get_employee(int(emp)):
        return _redirect("/hr/leave", "연결할 직원을 고르세요")
    leave.link_name(name, int(emp))
    return _redirect("/hr/leave", f"'{name}'을(를) 연결했습니다")


@router.get("/{emp_id}", response_class=HTMLResponse)
def leave_detail(request: Request, emp_id: int, year: int | None = None):
    year = _year(year)
    emp = repo.get_employee(emp_id)
    if not emp:
        return _redirect("/hr/leave", "임직원을 찾을 수 없습니다")
    s = leave.summary(emp, year)
    promos = leave.promotions(emp_id, year)
    base = base_url(request)
    for p in promos:
        p["link"] = f"{base}/leave-plan/{p['token']}" if p.get("token") and p["status"] == "회신대기" else ""
    audit("hr", "연차 상세 열람", f"emp#{emp_id}", f"{year}년")
    return _render(request, "hr/leave_detail.html", {
        "year": year, "s": s, "e": emp, "promos": promos, "sch": leave.schedule(year),
        "fy": leave.first_year_schedule(emp["hire_date"]) if s["ent"]["first_year"] else None,
        "mail_ready": mailer.configured(), "min_date": max(today(), to_date(f"{year}-01-01")).isoformat(),
        "max_date": f"{year}-12-31",
    })


@router.post("/{emp_id}/adjust")
async def leave_adjust(request: Request, emp_id: int):
    f = await request.form()
    year = _year(f.get("year"))
    try:
        days = float(f.get("days") or 0)
        used_before = float(f.get("used_before") or 0)
    except ValueError:
        return _redirect(f"/hr/leave/{emp_id}?year={year}", "숫자로 입력하세요")
    leave.save_adjust(emp_id, year, days, used_before, (f.get("note") or "").strip()[:200])
    return _redirect(f"/hr/leave/{emp_id}?year={year}", "조정값을 저장했습니다")


# ── 1차 촉진: 잔여 통보 + 사용 시기 회신 요청 ──────────────────────────
def _notice_html(s: dict, year: int, deadline: str, company: str) -> str:
    e, esc = s["emp"], html.escape
    return (
        '<h1 class="doc-title">연차휴가 사용 촉진 통보서 (1차)</h1>'
        '<table class="doc-table">'
        f'<tr><th>성명</th><td>{esc(e["name"])}</td><th>소속</th><td>{esc(e.get("department") or "")}</td></tr>'
        f'<tr><th>입사일</th><td>{esc(e["hire_date"])}</td><th>대상 연도</th><td>{year}년 (회계연도)</td></tr>'
        f'<tr><th>발생 연차</th><td>{_fmt(s["granted"])}일</td><th>사용 (통보일 기준)</th><td>{_fmt(s["used"])}일</td></tr>'
        f'<tr><th>미사용 잔여</th><td><b>{_fmt(s["remaining"])}일</b></td><th>사용 기한</th><td>{year}-12-31</td></tr>'
        f'<tr><th>통보일</th><td>{today().isoformat()}</td><th>회신 기한</th><td><b>{deadline}</b></td></tr>'
        '</table>'
        f'<p>근로기준법 제61조 제1항 제1호에 따라 귀하의 미사용 연차휴가 일수를 알려 드립니다. '
        f'이 통보를 받은 날부터 10일 이내({deadline}까지)에 미사용 연차휴가의 사용 시기를 정하여 회사에 통보해 주시기 바랍니다.</p>'
        f'<p>기한까지 사용 시기를 통보하지 않으시면 회사가 사용 시기를 정하여 서면으로 통보합니다(같은 조 제1항 제2호). '
        f'회사의 사용 촉진에도 불구하고 사용하지 않은 연차휴가는 {year}년 12월 31일에 소멸되며, '
        '이에 대하여는 미사용 연차휴가 수당이 지급되지 않을 수 있습니다.</p>'
        f'<p class="text-end">{today().strftime("%Y년 %m월 %d일")}<br><b>{esc(company)}</b></p>'
    )


def _make_doc(emp: dict, category: str, title: str, content: str) -> int:
    doc_id = repo.create_document(doc_group="leave", employee_id=emp["id"], category=category, title=title,
                                  content=content, status="발급완료", signed_date=today().isoformat())
    services.store_document_pdf(doc_id)      # 직원 폴더에 증빙 PDF (NAS 미설정이면 건너뜀)
    return doc_id


def send_first(emp_ids: list[int], year: int, base: str) -> dict:
    company = get_settings().get("company_name", "")
    deadline = (today() + timedelta(days=REPLY_DAYS)).isoformat()
    res = {"sent": [], "link_only": [], "skipped": []}
    for emp_id in emp_ids:
        emp = repo.get_employee(emp_id)
        if not emp:
            continue
        s = leave.summary(emp, year)
        if s["remaining"] <= 0:
            res["skipped"].append(f"{emp['name']}(잔여 없음)")
            continue
        if s["ent"]["first_year"]:          # 월차는 입사 1년이 되는 날 소멸 → 일정이 달라 일괄 통보하지 않음
            res["skipped"].append(f"{emp['name']}(입사 1년 미만: 상세 화면 일정 참고)")
            continue
        if not emp.get("email") or not emp.get("birth_date"):
            res["skipped"].append(f"{emp['name']}(이메일·생년월일 없음)")
            continue
        token = secrets.token_urlsafe(32)
        link = f"{base}/leave-plan/{token}"
        content = _notice_html(s, year, deadline, company)
        with batch():
            execute("UPDATE leave_promotions SET status='취소' WHERE employee_id=? AND year=? AND round=1 "
                    "AND status='회신대기'", (emp_id, year))
            pid = execute("INSERT INTO leave_promotions(employee_id, year, round, remaining, deadline, to_addr, token_hash, "
                          "token, status, sent_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (emp_id, year, 1, s["remaining"], deadline, enc(emp["email"]), _hash(token), enc(token),
                           "회신대기", current_actor.get()[1]))
        doc_id = _make_doc(emp, "연차촉진1차", f"{year}년 연차 사용 촉진 통보(1차) - {emp['name']}", content)
        execute("UPDATE leave_promotions SET doc_id=? WHERE id=?", (doc_id, pid))
        mailed = False
        if mailer.configured():
            body = (f"{emp['name']}님, 안녕하세요.\n\n근로기준법 제61조에 따라 {year}년 미사용 연차휴가를 알려 드립니다.\n\n"
                    f"  · 미사용 잔여: {_fmt(s['remaining'])}일 (사용 기한 {year}-12-31)\n"
                    f"  · 회신 기한: {deadline}\n\n"
                    f"아래 링크에서 생년월일 6자리로 본인 확인 후, 연차를 사용할 날짜를 골라 보내 주세요.\n{link}\n\n"
                    "기한까지 회신이 없으면 회사가 사용 시기를 정해 알려 드립니다. "
                    f"촉진에도 사용하지 않은 연차는 {year}년 12월 31일에 소멸되며 수당이 지급되지 않을 수 있습니다.\n\n"
                    "※ 실제 휴가 신청은 지금처럼 메일플러그 근태에서 해 주세요.\n"
                    f"통보서 사본(PDF)을 첨부합니다.\n\n{company} 경영관리팀")
            try:
                mailer.send(emp["email"], f"[{company}] {year}년 연차휴가 사용 촉진 안내 (1차)", body, _attach(doc_id))
                mailed = True
                execute("UPDATE leave_promotions SET mailed=1 WHERE id=?", (pid,))
            except mailer.MailError as ex:
                res.setdefault("errors", []).append(str(ex))
        (res["sent"] if mailed else res["link_only"]).append(emp["name"])
        audit("hr", "연차 촉진 1차", f"emp#{emp_id}", f"{year}년 잔여 {_fmt(s['remaining'])}일 · " + ("메일" if mailed else "링크만"))
    return res


def _attach(doc_id: int) -> list:
    from app import storage
    d = repo.get_document(doc_id)
    if not d or not d.get("pdf_path"):
        return []
    try:
        return [(f"{d['category']}_{d['name']}.pdf", storage.read_verified(d["pdf_path"], d["pdf_sha256"]))]
    except storage.StorageError:
        return []


@router.post("/promote")
async def leave_promote(request: Request):
    f = await request.form()
    year = _year(f.get("year"))
    ids = [int(v) for v in f.getlist("emp_ids") if str(v).isdigit()]
    if not ids:
        return _redirect(f"/hr/leave?year={year}", "촉진 메일을 보낼 직원을 고르세요")
    res = await run_in_threadpool(send_first, ids, year, base_url(request))
    parts = []
    if res["sent"]:
        parts.append(f"{len(res['sent'])}명에게 1차 촉진 메일을 보냈습니다")
    if res["link_only"]:
        parts.append(f"메일 미발송 {len(res['link_only'])}명({', '.join(res['link_only'][:5])}) — 직원 상세의 링크를 전달하세요")
    if res["skipped"]:
        parts.append("제외: " + ", ".join(res["skipped"][:8]))
    if res.get("errors"):
        parts.append("메일 오류: " + res["errors"][0])
    return _redirect(f"/hr/leave?year={year}", " / ".join(parts) or "보낼 대상이 없습니다")


@router.post("/promotions/{pid}/cancel")
def leave_promotion_cancel(pid: int):
    p = query_one("SELECT * FROM leave_promotions WHERE id=?", (pid,))
    if not p:
        return _redirect("/hr/leave", "기록이 없습니다")
    execute("UPDATE leave_promotions SET status='취소', session_hash=NULL WHERE id=?", (pid,))
    audit("hr", "연차 촉진 취소", f"emp#{p['employee_id']}", f"{p['year']}년 {p['round']}차")
    return _redirect(f"/hr/leave/{p['employee_id']}?year={p['year']}", "촉진 기록을 취소했습니다 (링크도 막힘)")


# ── 2차 촉진: 회사가 사용 시기 지정 ─────────────────────────────────────
def parse_plan_rows(dates: list, parts: list, year: int, min_date: str) -> tuple[list[tuple[str, str]], str]:
    rows, seen = [], set()
    for d, p in zip(dates, parts):
        d = (d or "").strip()
        if not d:
            continue
        dd = to_date(d)
        if not dd or not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
            return [], f"날짜 형식이 맞지 않습니다: {d}"
        if p not in leave.PARTS:
            return [], "종일/오전/오후 중에서 고르세요"
        if dd.year != year or d < min_date:
            return [], f"{d}: {min_date} ~ {year}-12-31 사이 날짜만 고를 수 있습니다"
        if dd.weekday() >= 5:
            return [], f"{d}: 주말은 고를 수 없습니다"
        if d in seen:
            return [], f"{d}: 같은 날짜가 두 번 들어갔습니다"
        seen.add(d)
        rows.append((d, p))
    if len(rows) > MAX_PLAN_ROWS:
        return [], "날짜가 너무 많습니다"
    return sorted(rows), ""


@router.post("/{emp_id}/designate")
async def leave_designate(request: Request, emp_id: int):
    f = await request.form()
    year = _year(f.get("year"))
    back = f"/hr/leave/{emp_id}?year={year}"
    emp = repo.get_employee(emp_id)
    if not emp:
        return _redirect("/hr/leave", "임직원을 찾을 수 없습니다")
    rows, err = parse_plan_rows(f.getlist("plan_date"), f.getlist("plan_part"), year, today().isoformat())
    if err or not rows:
        return _redirect(back, err or "지정할 날짜를 입력하세요")
    s = leave.summary(emp, year)
    total = sum(leave.PARTS[p] for _, p in rows)
    if total > s["remaining"]:
        return _redirect(back, f"지정 일수({_fmt(total)}일)가 잔여({_fmt(s['remaining'])}일)보다 많습니다")
    company = get_settings().get("company_name", "")
    esc = html.escape
    lines = "".join(f"<tr><td>{d}</td><td>{p}</td></tr>" for d, p in rows)
    content = (
        '<h1 class="doc-title">연차휴가 사용 시기 지정 통보서 (2차)</h1>'
        '<table class="doc-table">'
        f'<tr><th>성명</th><td>{esc(emp["name"])}</td><th>소속</th><td>{esc(emp.get("department") or "")}</td></tr>'
        f'<tr><th>대상 연도</th><td>{year}년</td><th>미사용 잔여</th><td>{_fmt(s["remaining"])}일</td></tr>'
        f'<tr><th>지정 일수</th><td>{_fmt(total)}일</td><th>통보일</th><td>{today().isoformat()}</td></tr></table>'
        f'<table class="doc-table"><tr><th>지정일</th><th>구분</th></tr>{lines}</table>'
        '<p>근로기준법 제61조 제1항 제2호에 따라, 1차 촉진 후 사용 시기를 통보하지 않은 미사용 연차휴가의 사용 시기를 '
        '위와 같이 지정하여 통보합니다. 지정일에 연차휴가를 사용하시기 바랍니다.</p>'
        f'<p class="text-end">{today().strftime("%Y년 %m월 %d일")}<br><b>{esc(company)}</b></p>'
    )

    def work():
        with batch():
            pid = execute("INSERT INTO leave_promotions(employee_id, year, round, remaining, to_addr, status, sent_by) "
                          "VALUES (?,?,?,?,?,?,?)", (emp_id, year, 2, s["remaining"], enc(emp.get("email") or ""),
                                                      "지정통보", current_actor.get()[1]))
            for d, p in rows:
                execute("INSERT INTO leave_plans(employee_id, year, plan_date, part, source, promotion_id) VALUES (?,?,?,?,?,?)",
                        (emp_id, year, d, p, "회사지정", pid))
        doc_id = _make_doc(emp, "연차촉진2차", f"{year}년 연차 사용 시기 지정 통보(2차) - {emp['name']}", content)
        execute("UPDATE leave_promotions SET doc_id=? WHERE id=?", (doc_id, pid))
        msg = f"사용 시기 {len(rows)}일을 지정했습니다"
        if mailer.configured() and emp.get("email"):
            body = (f"{emp['name']}님, 안녕하세요.\n\n근로기준법 제61조 제1항 제2호에 따라 {year}년 미사용 연차휴가의 사용 시기를 "
                    "아래와 같이 지정하여 알려 드립니다.\n\n" + "\n".join(f"  · {d} {p}" for d, p in rows) +
                    "\n\n지정일에 메일플러그 근태에서 휴가를 신청해 주세요. 통보서 사본(PDF)을 첨부합니다.\n\n"
                    f"{company} 경영관리팀")
            try:
                mailer.send(emp["email"], f"[{company}] {year}년 연차휴가 사용 시기 지정 통보 (2차)", body, _attach(doc_id))
                execute("UPDATE leave_promotions SET mailed=1 WHERE id=?", (pid,))
                msg += " · 메일 발송"
            except mailer.MailError as ex:
                msg += f" · 메일 실패({ex}) — 통보서를 출력해 전달하세요"
        else:
            msg += " · 메일 미발송 — 통보서를 출력해 전달하세요"
        if today() > leave.schedule(year)["second_by"]:
            msg += f" · 주의: 2차 지정 기한({year}-10-31)이 지났습니다"
        audit("hr", "연차 촉진 2차", f"emp#{emp_id}", f"{year}년 {_fmt(total)}일 지정")
        return msg

    return _redirect(back, await run_in_threadpool(work))


# ════════════════════════════════════════════════════════════════════════
# 직원 회신 화면 (/leave-plan/{token})
# ════════════════════════════════════════════════════════════════════════
def _page(request, name, ctx):
    ctx.setdefault("subtitle", "연차 사용 계획")
    return templates.TemplateResponse(request, name, ctx)


def _message(request, title, text):
    return _page(request, "sign/message.html", {"title": title, "text": text})


def _load(token: str) -> tuple[dict | None, str]:
    p = query_one("SELECT * FROM leave_promotions WHERE token_hash=?", (_hash(token),))
    if not p or p["status"] not in ("회신대기", "회신완료"):
        return None, "유효하지 않은 링크입니다. 경영관리팀에 문의해 주세요."
    if (p["deadline"] or "") < today().isoformat():
        return None, "회신 기한이 지났습니다. 경영관리팀에 문의해 주세요."
    if p["fail_count"] >= MAX_FAILS:
        return None, "본인 확인을 여러 번 실패해 링크가 잠겼습니다. 경영관리팀에 문의해 주세요."
    return p, ""


def _verified(request: Request, p: dict) -> bool:
    c = request.cookies.get(COOKIE, "")
    return bool(c and p["session_hash"] == _hash(c) and (p["session_expires"] or "") > _now())


@public_router.get("/{token}", response_class=HTMLResponse)
def plan_page(request: Request, token: str):
    p, problem = _load(token)
    if problem:
        return _message(request, "연차 사용 계획", problem)
    if not _verified(request, p):
        return _page(request, "sign/verify.html", {"token": token, "error": request.query_params.get("e", ""),
                                                   "verify_action": f"/leave-plan/{token}/verify"})
    emp = repo.get_employee(p["employee_id"])
    execute("UPDATE leave_promotions SET opened_at=COALESCE(opened_at, ?) WHERE id=?", (_now(), p["id"]))
    mine = [x for x in leave.plans(emp["id"], p["year"]) if x["source"] == "직원회신"]
    return _page(request, "sign/leave_plan.html", {
        "token": token, "p": p, "name": emp["name"], "plans": mine, "parts": list(leave.PARTS),
        "min_date": max(today().isoformat(), f"{p['year']}-01-01"), "max_date": f"{p['year']}-12-31",
        "remaining": p["remaining"], "error": request.query_params.get("e", ""),
    })


@public_router.post("/{token}/verify")
async def plan_verify(request: Request, token: str):
    p, problem = _load(token)
    if problem:
        return _message(request, "연차 사용 계획", problem)
    form = await request.form()
    emp = repo.get_employee(p["employee_id"])
    expected = (emp.get("birth_date") or "").replace("-", "")[2:]
    given = re.sub(r"\D", "", str(form.get("birth", "")))
    if not expected or not secrets.compare_digest(given.encode(), expected.encode()):
        execute("UPDATE leave_promotions SET fail_count=fail_count+1 WHERE id=?", (p["id"],))
        audit("hr", "연차 회신 본인확인 실패", f"emp#{emp['id']}", "")
        return RedirectResponse(f"/leave-plan/{token}?e=" + quote("생년월일이 일치하지 않습니다"), status_code=303)
    session = secrets.token_urlsafe(32)
    expires = (datetime.now() + timedelta(minutes=SESSION_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
    execute("UPDATE leave_promotions SET session_hash=?, session_expires=?, fail_count=0 WHERE id=?",
            (_hash(session), expires, p["id"]))
    res = RedirectResponse(f"/leave-plan/{token}", status_code=303)
    res.set_cookie(COOKIE, session, max_age=SESSION_MINUTES * 60, httponly=True, samesite="strict",
                   secure=settings.secure_cookie, path="/leave-plan")
    return res


def _save_reply(p: dict, rows: list, ip: str) -> str:
    emp = repo.get_employee(p["employee_id"])
    year, now = p["year"], _now()
    total = sum(leave.PARTS[x] for _, x in rows)
    esc = html.escape
    lines = "".join(f"<tr><td>{d}</td><td>{x}</td></tr>" for d, x in rows)
    content = (
        '<h1 class="doc-title">연차휴가 사용 시기 통보 (직원 회신)</h1>'
        '<table class="doc-table">'
        f'<tr><th>성명</th><td>{esc(emp["name"])}</td><th>소속</th><td>{esc(emp.get("department") or "")}</td></tr>'
        f'<tr><th>대상 연도</th><td>{year}년</td><th>촉진 통보 잔여</th><td>{_fmt(p["remaining"])}일</td></tr>'
        f'<tr><th>계획 일수</th><td>{_fmt(total)}일</td><th>회신 일시</th><td>{now}</td></tr>'
        f'<tr><th>접속 IP</th><td colspan="3">{esc(ip)}</td></tr></table>'
        f'<table class="doc-table"><tr><th>사용 예정일</th><th>구분</th></tr>{lines}</table>'
        '<p>위 근로자는 회사의 연차휴가 사용 촉진(근로기준법 제61조 제1항 제1호)에 따라 생년월일로 본인 확인 후 '
        '사용 시기를 전자적으로 통보하였습니다.</p>'
    )
    with batch():
        execute("DELETE FROM leave_plans WHERE employee_id=? AND year=? AND source='직원회신'", (emp["id"], year))
        for d, x in rows:
            execute("INSERT INTO leave_plans(employee_id, year, plan_date, part, source, promotion_id) VALUES (?,?,?,?,?,?)",
                    (emp["id"], year, d, x, "직원회신", p["id"]))
        execute("UPDATE leave_promotions SET status='회신완료', responded_at=?, responder_ip=? WHERE id=?", (now, ip, p["id"]))
        audit("hr", "연차 사용계획 회신", f"emp#{emp['id']}", f"{year}년 {len(rows)}일({_fmt(total)}일)")
    _make_doc(emp, "연차사용계획", f"{year}년 연차 사용 시기 통보(회신) - {emp['name']}", content)
    if mailer.configured() and emp.get("email"):
        company = get_settings().get("company_name", "")
        try:
            mailer.send(emp["email"], f"[{company}] 연차 사용 계획이 접수되었습니다",
                        f"{emp['name']}님, 아래 날짜로 연차 사용 계획이 접수되었습니다.\n\n" +
                        "\n".join(f"  · {d} {x}" for d, x in rows) +
                        "\n\n실제 휴가는 해당 날짜에 맞춰 메일플러그 근태에서 신청해 주세요.\n"
                        f"회신 기한({p['deadline']})까지는 같은 링크에서 다시 고칠 수 있습니다.\n\n{company} 경영관리팀")
        except mailer.MailError:
            pass
    return f"{len(rows)}일({_fmt(total)}일) 사용 계획이 접수되었습니다. 실제 휴가는 메일플러그 근태에서 신청해 주세요."


@public_router.post("/{token}/submit")
async def plan_submit(request: Request, token: str):
    p, problem = _load(token)
    if problem:
        return _message(request, "연차 사용 계획", problem)
    if not _verified(request, p):
        return RedirectResponse(f"/leave-plan/{token}", status_code=303)
    form = await request.form()
    rows, err = parse_plan_rows(form.getlist("plan_date"), form.getlist("plan_part"), p["year"],
                                max(today().isoformat(), f"{p['year']}-01-01"))
    if not err and not rows:
        err = "사용할 날짜를 하나 이상 입력하세요"
    if not err and sum(leave.PARTS[x] for _, x in rows) > p["remaining"]:
        err = f"잔여 연차({_fmt(p['remaining'])}일)보다 많이 고를 수 없습니다"
    if err:
        return RedirectResponse(f"/leave-plan/{token}?e=" + quote(err), status_code=303)
    text = await run_in_threadpool(_save_reply, p, rows, request.state.ip)
    return _message(request, "회신 완료", text)
