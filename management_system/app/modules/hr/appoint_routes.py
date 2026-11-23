"""인사발령 화면 (/hr/appointments). 관리자·인사담당만 (middleware RULES)."""
import csv
import io
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse

from app.common import templates, to_date, today
from app.db import audit, get_codes, query
from app import company
from app.modules.hr import appoint
from app.storage import StorageError, render_pdf

router = APIRouter(prefix="/hr/appointments")


def _go(msg: str = "", **params) -> RedirectResponse:
    q = {k: v for k, v in params.items() if v}
    if msg:
        q["msg"] = msg
    return RedirectResponse("/hr/appointments" + ("?" + urlencode(q) if q else ""), status_code=303)


def _filters(request: Request) -> dict:
    qp = request.query_params
    f = {k: (qp.get(k) or "").strip()[:100] for k in ("date_from", "date_to", "department", "appt_type", "batch", "q")}
    for k in ("date_from", "date_to"):
        f[k] = to_date(f[k]).isoformat() if to_date(f[k]) else ""
    return f


def _rows(f: dict) -> list[dict]:
    return appoint.rows(f["date_from"], f["date_to"], f["department"], f["appt_type"], f["batch"], f["q"])


@router.get("", response_class=HTMLResponse)
def page(request: Request):
    appoint.apply_due()
    f = _filters(request)
    rows = _rows(f)
    t = today().isoformat()
    return templates.TemplateResponse(request, "hr/appointments.html", {
        "rows": rows, "f": f, "batches": appoint.batches(), "movement": appoint.movement(rows) if (f["batch"] or f["date_from"]) else [],
        "fields": appoint.FIELDS, "codes": {k: get_codes(g) for k, (_n, _t, g) in appoint.FIELDS.items()},
        "types": sorted({r["appt_type"] for r in query("SELECT DISTINCT appt_type FROM appointments")}),
        "depts": get_codes("DEPT"), "today_iso": t,
        "planned": sum(1 for r in rows if r["planned"]),
        "emps": [e for e in query("SELECT id, name, emp_no, department, rank, title, work_location, resign_date FROM employees WHERE 1=1" + company.sql("") +
                                  " ORDER BY department, name") if not e.get("resign_date") or e["resign_date"] >= t],
        "query": urlencode({k: v for k, v in f.items() if v}),
        "active_menu": "hr.appointments", "msg": request.query_params.get("msg", ""),
    })


@router.post("/register")
async def register(request: Request):
    form = await request.form()
    u = request.state.user
    try:
        r = await run_in_threadpool(
            appoint.register, [int(v) for v in form.getlist("emp") if str(v).isdigit()], str(form.get("appt_date") or ""),
            {k: str(form.get("to_" + k) or "") for k in appoint.FIELDS}, str(form.get("batch_title") or ""),
            str(form.get("note") or ""), str(form.get("mode") or "apply"),
            {k: str(form.get("from_" + k) or "") for k in appoint.FIELDS}, u.get("display_name") or u.get("username") or "")
    except appoint.AppointError as ex:
        return _go(str(ex))
    msg = (f"발령 {r['added']}건을 예정으로 등록했습니다. 발령일에 자동으로 적용됩니다" if r["planned"]
           else f"발령 {r['added']}건을 등록했습니다") + (f" (이미 같은 값이라 뺀 것 {r['skipped']}건)" if r["skipped"] else "")
    return _go(msg, batch=str(form.get("batch_title") or "").strip()[:100])


@router.post("/remove-selected")
async def remove_selected(request: Request):
    """목록에서 체크한 발령 기록을 한 번에 삭제 (인사·고용 정보는 그대로)."""
    form = await request.form()
    ids = [int(v) for v in form.getlist("ids") if str(v).isdigit()]
    back = {k: str(form.get(k) or "").strip()[:100] for k in ("date_from", "date_to", "department", "appt_type", "batch", "q")}
    if not ids:
        return _go("삭제할 발령을 체크해 주세요", **back)
    r = await run_in_threadpool(appoint.delete_many, ids)
    msg = f"발령 기록 {r['deleted']}건을 삭제했습니다" + (f" (예정 취소 {r['planned']}건 포함)" if r["planned"] else "") + ". 인사·고용 정보는 그대로입니다"
    return _go(msg, **back)


@router.post("/{appt_id}/remove")
def remove(appt_id: int):
    return _go(appoint.delete(appt_id) or "기록을 찾을 수 없습니다")


@router.get("/export.csv")
def export(request: Request):
    f = _filters(request)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["발령일", "상태", "묶음 이름", "사번", "성명", "구분", "변경 전", "변경 후", "비고"])
    for r in _rows(f):
        w.writerow([r["appt_date"], r.get("status") or "적용", r.get("batch_title") or "", r.get("emp_no") or "", r["name"],
                    r["appt_type"], r.get("from_value") or "", r.get("to_value") or "", r.get("note") or ""])
    audit("hr", "인사발령 현황 내려받기", f["batch"] or f["date_from"] or "전체", "")
    fname = quote(f"인사발령_{today().isoformat()}.csv")
    return StreamingResponse(iter(["﻿" + buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f"attachment; filename*=UTF-8''{fname}"})


@router.get("/notice.pdf")
async def notice(request: Request):
    """지금 조회한 발령(묶음·기간)을 인사발령 통지 문서로."""
    f = _filters(request)
    if not (f["batch"] or f["date_from"]):
        return _go("통지 문서는 발령 묶음을 고르거나 기간을 정한 뒤 만들 수 있습니다")
    rows = [r for r in _rows(f) if r["appt_type"] not in ("입사", "퇴사")]
    if not rows:
        return _go("해당하는 발령이 없습니다", **{k: v for k, v in f.items() if k != "q"})
    title = f["batch"] or f"{f['date_from']} ~ {f['date_to'] or today().isoformat()} 인사발령"
    try:
        pdf = await run_in_threadpool(render_pdf, appoint.notice_html(rows, title), "인사발령")
    except StorageError as ex:
        return _go(str(ex))
    audit("hr", "인사발령 통지 문서", title, f"{len(rows)}건")
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote('인사발령_' + today().isoformat() + '.pdf')}"})
