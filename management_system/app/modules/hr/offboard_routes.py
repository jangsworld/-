"""퇴사·입사 절차 안내 화면 (/hr/offboard, /hr/onboard). 관리자·인사담당만 (middleware RULES).

두 화면은 하는 일이 같아 한 틀(make_router)로 만들고, 쓰는 자료 모듈(offboard / onboard)과 화면 글자(L)만 다릅니다.
"""
import re
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse

from app import mailer
from app.common import templates, today
from app.db import get_settings, query
from app.modules.hr import offboard, onboard, repo, richtext

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")

LABELS = {
    "offboard": {
        "name": "퇴사", "title": "퇴사 절차 안내", "icon": "bi-box-arrow-right", "day": "퇴직 예정일", "due": "인수인계 기한", "person": "인수자",
        "target": "퇴사 예정 직원", "start_btn": "퇴사 안내 시작", "when_ex": "퇴직일 전까지",
        "start_help": "사직서가 대표이사 결재까지 끝나면 여기서 시작합니다. 인사기록카드의 [퇴사 안내] 버튼으로도 들어올 수 있습니다.",
        "day_help": "퇴직 예정일은 이 화면에만 저장됩니다. 인사기록카드의 퇴사일은 실제 퇴직일에 따로 입력하세요(미리 넣으면 재직자에서 빠집니다).",
        "print_title": "퇴직 절차 안내", "print_lead": "퇴직일까지 챙기실 사항을 순서대로 정리했습니다.",
        "print_foot": "이 안내문은 퇴직 절차를 돕기 위해 드리는 것입니다.", "due_default": "퇴직일 전까지", "person_default": "부서장이 지정",
    },
    "onboard": {
        "name": "입사", "title": "입사 절차 안내", "icon": "bi-box-arrow-in-right", "day": "입사일", "due": "서류 제출 기한", "person": "안내 담당",
        "target": "입사 예정 직원", "start_btn": "입사 안내 시작", "when_ex": "입사일까지",
        "start_help": "입사가 확정되면 인사기록카드에 먼저 등록(입사일 입력)한 뒤 여기서 시작합니다. 최근에 입사한 직원의 인사기록카드 [입사 안내] 버튼으로도 들어올 수 있습니다.",
        "day_help": "인사기록카드의 입사일을 기본으로 보여 줍니다. 여기서 고친 날짜는 안내문에만 쓰이고 인사기록카드는 바뀌지 않습니다.",
        "print_title": "입사 안내", "print_lead": "첫 출근 전에 준비하실 것과 입사일 일정을 정리했습니다.",
        "print_foot": "이 안내문은 입사 준비를 돕기 위해 드리는 것입니다.", "due_default": "입사일까지", "person_default": "부서 담당자",
    },
}


def _mask(v: str) -> str:
    name, _, dom = (v or "").partition("@")
    return (name[:2] + "***@" + dom) if dom else (v or "")


def _sender(request: Request) -> str:
    u = request.state.user
    return f"경영관리팀 {u.get('name') or u.get('username') or ''}".strip()


def _emp(emp_id: int) -> dict:
    e = repo.get_employee(emp_id)
    if not e:
        raise HTTPException(404, "임직원을 찾을 수 없습니다")
    return e


def make_router(kind: str, flow) -> APIRouter:
    base, L, menu = f"/hr/{kind}", LABELS[kind], f"hr.{kind}"
    router = APIRouter(prefix=base)

    def go(emp_id: int | None, msg: str = "") -> RedirectResponse:
        url = base + (f"/{emp_id}" if emp_id else "")
        return RedirectResponse(url + ("?msg=" + quote(msg) if msg else ""), status_code=303)

    @router.get("", response_class=HTMLResponse)
    def page(request: Request):
        t = today().isoformat()
        emps = [e for e in query("SELECT id, name, emp_no, department, rank, hire_date, resign_date FROM employees ORDER BY name")
                if not e.get("resign_date") or e["resign_date"] >= t]
        if kind == "onboard":                       # 입사: 최근에 들어왔거나 들어올 사람이 위로
            emps.sort(key=lambda e: e.get("hire_date") or "", reverse=True)
        return templates.TemplateResponse(request, "hr/offboard.html", {
            "rows": flow.rows(), "stages": flow.stages(), "stages_custom": flow.stages_custom(),
            "notice_key": flow.NOTICE_KEY, "tpl": flow.templates_text(), "fields": flow.FIELDS, "emps": emps,
            "base": base, "L": L, "kind": kind, "active_menu": menu, "msg": request.query_params.get("msg", ""),
        })

    @router.post("/template")
    async def template_save(request: Request):
        f = await request.form()
        err = flow.save_template(f.get("subject") or "", f.get("body") or "", reset=bool(f.get("reset")))
        return go(None, err or ("처음 글귀로 되돌렸습니다" if f.get("reset") else "안내문 기본 글귀를 저장했습니다"))

    @router.post("/stages")
    async def stages_save(request: Request):
        body = await request.json()
        reset = bool(body.get("reset")) if isinstance(body, dict) else False
        err = flow.save_stages(body.get("stages") if isinstance(body, dict) else None, reset=reset)
        if err:
            raise HTTPException(400, err)
        return {"ok": True, "msg": "처음 단계로 되돌렸습니다" if reset else f"{L['name']} 처리 단계를 저장했습니다"}

    @router.get("/start")
    def start(emp: int = 0):
        return go(emp) if emp and repo.get_employee(emp) else go(None, "직원을 선택하세요")

    @router.get("/{emp_id}", response_class=HTMLResponse)
    def detail(emp_id: int, request: Request):
        e = _emp(emp_id)
        ob = flow.get(emp_id)
        tpl = flow.templates_text()
        sender = _sender(request)
        email = (e.get("email") or "").strip()
        keys, checks = flow.keys(), flow.checks(emp_id)
        day = (ob or {}).get("last_day") or (e.get("hire_date") if kind == "onboard" else "")
        return templates.TemplateResponse(request, "hr/offboard_detail.html", {
            "e": e, "ob": ob, "stages": flow.stages(), "checks": checks, "total": len(keys), "done": len([k for k in checks if k in keys]),
            "dates": flow.dates(ob), "subject": flow.fill(tpl["subject"], e, ob, sender),
            "body": flow.fill(tpl["body"], e, ob, sender, html=True),
            "mail_ready": mailer.configured(), "email_ok": bool(_EMAIL_RE.match(email)), "email_masked": _mask(email),
            "mails": [{**m, "to_masked": _mask(m.get("to_addr") or ""),
                       "body_html": richtext.clean(m.get("body") or "") if m.get("fmt") == "html" else ""} for m in flow.mails(emp_id)],
            "today_iso": today().isoformat(), "co_name": get_settings().get("company_name", ""), "sender": sender,
            "day_value": day or "", "last_day_k": flow._kdate(day), "due_k": flow._kdate((ob or {}).get("handover_due")),
            "base": base, "L": L, "kind": kind, "active_menu": menu, "msg": request.query_params.get("msg", ""),
        })

    @router.post("/{emp_id}/save")
    async def save(emp_id: int, request: Request):
        _emp(emp_id)
        f = await request.form()
        err = flow.save(emp_id, f.get("last_day"), f.get("handover_due"), f.get("successor") or "", f.get("note") or "")
        return go(emp_id, err or "저장했습니다. 안내문에 날짜가 반영되었습니다")

    @router.post("/{emp_id}/check")
    async def check(emp_id: int, request: Request):
        body = await request.json()
        if not flow.set_check(emp_id, str(body.get("key") or ""), bool(body.get("done"))):
            raise HTTPException(400, f"{L['day']}을 먼저 저장하세요")
        keys = flow.keys()
        return {"ok": True, "done": len([k for k in flow.checks(emp_id) if k in keys])}

    @router.post("/{emp_id}/send")
    async def send(emp_id: int, request: Request):
        e = _emp(emp_id)
        if not flow.get(emp_id):
            return go(emp_id, f"{L['day']}을 먼저 저장하세요")
        f = await request.form()
        subject, body, target = f.get("subject") or "", f.get("body") or "", f.get("target") or "employee"
        if target == "hand":       # 인쇄·메신저 등으로 직접 전달하고 기록만 남김
            err = flow.notice_error(subject, body)
            if err:
                return go(emp_id, err)
            flow.log_notice(emp_id, "직접 전달", "", subject, body)
            return go(emp_id, "직접 전달한 것으로 기록했습니다")
        if target == "manual":
            to, how = (f.get("manual_to") or "").strip(), "메일(직접입력)"
            if not _EMAIL_RE.match(to):
                return go(emp_id, "받는 사람 메일 주소를 올바르게 입력하세요 (한 명만)")
        else:
            to, how = (e.get("email") or "").strip(), "메일(인사기록카드)"
            if not _EMAIL_RE.match(to):
                return go(emp_id, "인사기록카드에 이메일이 없거나 형식이 맞지 않습니다. 주소를 직접 입력해 보내세요")
        ok, msg = await run_in_threadpool(flow.send, emp_id, to, how, subject, body)
        return go(emp_id, f"{_mask(to)} 로 안내문을 보냈습니다" if ok else msg)

    @router.post("/{emp_id}/close")
    async def close(emp_id: int, request: Request):
        f = await request.form()
        if flow.get(emp_id):
            flow.set_closed(emp_id, f.get("reopen") is None)
        return go(emp_id, "다시 진행 중으로 바꿨습니다" if f.get("reopen") is not None else f"{L['name']} 절차를 완료로 표시했습니다")

    @router.post("/{emp_id}/delete")
    def delete(emp_id: int):
        flow.remove(emp_id)
        return go(None, f"{L['name']} 절차 기록을 삭제했습니다 (인사기록카드는 그대로입니다)")

    return router


router = make_router("offboard", offboard)
onboard_router = make_router("onboard", onboard)
