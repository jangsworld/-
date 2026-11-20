"""퇴사 절차 안내 화면 (/hr/offboard). 관리자·인사담당만 (middleware RULES)."""
import re
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse

from app import mailer
from app.common import templates, today
from app.db import get_settings, query
from app.modules.hr import offboard, repo, richtext

router = APIRouter(prefix="/hr/offboard")
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def _go(emp_id: int | None, msg: str = "") -> RedirectResponse:
    url = "/hr/offboard" + (f"/{emp_id}" if emp_id else "")
    return RedirectResponse(url + ("?msg=" + quote(msg) if msg else ""), status_code=303)


def _emp(emp_id: int) -> dict:
    e = repo.get_employee(emp_id)
    if not e:
        raise HTTPException(404, "임직원을 찾을 수 없습니다")
    return e


def _mask(v: str) -> str:
    name, _, dom = (v or "").partition("@")
    return (name[:2] + "***@" + dom) if dom else (v or "")


def _sender(request: Request) -> str:
    u = request.state.user
    return f"경영관리팀 {u.get('name') or u.get('username') or ''}".strip()


@router.get("", response_class=HTMLResponse)
def page(request: Request):
    t = today().isoformat()
    return templates.TemplateResponse(request, "hr/offboard.html", {
        "rows": offboard.rows(), "stages": offboard.stages(), "stages_custom": offboard.stages_custom(),
        "notice_key": offboard.NOTICE_KEY, "tpl": offboard.templates_text(), "fields": offboard.FIELDS,
        "emps": [e for e in query("SELECT id, name, emp_no, department, rank, resign_date FROM employees ORDER BY name")
                 if not e.get("resign_date") or e["resign_date"] >= t],
        "active_menu": "hr.offboard", "msg": request.query_params.get("msg", ""),
    })


@router.post("/template")
async def template_save(request: Request):
    f = await request.form()
    err = offboard.save_template(f.get("subject") or "", f.get("body") or "", reset=bool(f.get("reset")))
    return _go(None, err or ("처음 글귀로 되돌렸습니다" if f.get("reset") else "안내문 기본 글귀를 저장했습니다"))


@router.post("/stages")
async def stages_save(request: Request):
    body = await request.json()
    reset = bool(body.get("reset")) if isinstance(body, dict) else False
    err = offboard.save_stages(body.get("stages") if isinstance(body, dict) else None, reset=reset)
    if err:
        raise HTTPException(400, err)
    return {"ok": True, "msg": "처음 단계로 되돌렸습니다" if reset else "퇴사 처리 단계를 저장했습니다"}


@router.get("/start")
def start(emp: int = 0):
    return _go(emp) if emp and repo.get_employee(emp) else _go(None, "직원을 선택하세요")


@router.get("/{emp_id}", response_class=HTMLResponse)
def detail(emp_id: int, request: Request):
    e = _emp(emp_id)
    ob = offboard.get(emp_id)
    tpl = offboard.templates_text()
    sender = _sender(request)
    email = (e.get("email") or "").strip()
    keys, checks = offboard.keys(), offboard.checks(emp_id)
    return templates.TemplateResponse(request, "hr/offboard_detail.html", {
        "e": e, "ob": ob, "stages": offboard.stages(), "checks": checks, "total": len(keys), "done": len([k for k in checks if k in keys]),
        "dates": offboard.dates(ob), "subject": offboard.fill(tpl["subject"], e, ob, sender),
        "body": offboard.fill(tpl["body"], e, ob, sender, html=True),
        "mail_ready": mailer.configured(), "email_ok": bool(_EMAIL_RE.match(email)), "email_masked": _mask(email),
        "mails": [{**m, "to_masked": _mask(m.get("to_addr") or ""),
                   "body_html": richtext.clean(m.get("body") or "") if m.get("fmt") == "html" else ""} for m in offboard.mails(emp_id)],
        "today_iso": today().isoformat(), "co_name": get_settings().get("company_name", ""), "sender": sender,
        "last_day_k": offboard._kdate((ob or {}).get("last_day")), "due_k": offboard._kdate((ob or {}).get("handover_due")),
        "active_menu": "hr.offboard", "msg": request.query_params.get("msg", ""),
    })


@router.post("/{emp_id}/save")
async def save(emp_id: int, request: Request):
    _emp(emp_id)
    f = await request.form()
    err = offboard.save(emp_id, f.get("last_day"), f.get("handover_due"), f.get("successor") or "", f.get("note") or "")
    return _go(emp_id, err or "저장했습니다. 안내문에 날짜가 반영되었습니다")


@router.post("/{emp_id}/check")
async def check(emp_id: int, request: Request):
    body = await request.json()
    if not offboard.set_check(emp_id, str(body.get("key") or ""), bool(body.get("done"))):
        raise HTTPException(400, "퇴직 예정일을 먼저 저장하세요")
    keys = offboard.keys()
    return {"ok": True, "done": len([k for k in offboard.checks(emp_id) if k in keys])}


@router.post("/{emp_id}/send")
async def send(emp_id: int, request: Request):
    e = _emp(emp_id)
    if not offboard.get(emp_id):
        return _go(emp_id, "퇴직 예정일을 먼저 저장하세요")
    f = await request.form()
    subject, body, target = f.get("subject") or "", f.get("body") or "", f.get("target") or "employee"
    if target == "hand":       # 인쇄·메신저 등으로 직접 전달하고 기록만 남김
        err = offboard.notice_error(subject, body)
        if err:
            return _go(emp_id, err)
        offboard.log_notice(emp_id, "직접 전달", "", subject, body)
        return _go(emp_id, "직접 전달한 것으로 기록했습니다")
    if target == "manual":
        to, how = (f.get("manual_to") or "").strip(), "메일(직접입력)"
        if not _EMAIL_RE.match(to):
            return _go(emp_id, "받는 사람 메일 주소를 올바르게 입력하세요 (한 명만)")
    else:
        to, how = (e.get("email") or "").strip(), "메일(인사기록카드)"
        if not _EMAIL_RE.match(to):
            return _go(emp_id, "인사기록카드에 이메일이 없거나 형식이 맞지 않습니다. 주소를 직접 입력해 보내세요")
    ok, msg = await run_in_threadpool(offboard.send, emp_id, to, how, subject, body)
    return _go(emp_id, f"{_mask(to)} 로 안내문을 보냈습니다" if ok else msg)


@router.post("/{emp_id}/close")
async def close(emp_id: int, request: Request):
    f = await request.form()
    if offboard.get(emp_id):
        offboard.set_closed(emp_id, f.get("reopen") is None)
    return _go(emp_id, "다시 진행 중으로 바꿨습니다" if f.get("reopen") is not None else "퇴사 절차를 완료로 표시했습니다")


@router.post("/{emp_id}/delete")
def delete(emp_id: int):
    offboard.remove(emp_id)
    return _go(None, "퇴사 절차 기록을 삭제했습니다 (인사기록카드는 그대로입니다)")
