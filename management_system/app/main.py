"""경영관리업무시스템 FastAPI 애플리케이션."""
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from urllib.parse import quote

from app.common import templates
from app.db import BASE_DIR, init_db, get_settings, set_setting, DEFAULT_SETTINGS
from app.modules import MODULES
from app.modules.hr import repo as hr_repo
from app.modules.hr.routes import router as hr_router



@asynccontextmanager
async def lifespan(_app):
    from app.seed import ensure_templates, seed_sample
    is_new = init_db()
    ensure_templates()
    if is_new and os.environ.get("MGMT_SEED", "1") == "1":
        seed_sample()
    yield


app = FastAPI(title="경영관리업무시스템", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
app.include_router(hr_router)

templates.env.globals["MODULES"] = MODULES
templates.env.globals["company"] = lambda: get_settings().get("company_name", "")


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/hr/dashboard")


SETTING_LABELS = {
    "company_name": "회사명", "ceo_name": "대표자", "company_address": "회사 주소",
    "business_no": "사업자등록번호", "company_phone": "대표 전화", "pay_day": "임금지급일(매월)",
    "min_wage_hourly": "최저임금(시간급, 원)", "monthly_std_hours": "월 소정근로시간(시간)",
    "alert_days": "만료·복직 사전알림(일)",
}


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    return templates.TemplateResponse(request, "settings.html", {
        "s": get_settings(), "labels": SETTING_LABELS, "codes": hr_repo.list_codes_all(),
        "logs": hr_repo.recent_audit(50), "active_menu": "settings",
        "msg": request.query_params.get("msg", ""),
        "groups": {"DEPT": "부서", "RANK": "직급", "TITLE": "직책", "LOCATION": "근무지"},
    })


@app.post("/settings")
async def settings_save(request: Request):
    form = await request.form()
    for k in DEFAULT_SETTINGS:
        if k in form:
            set_setting(k, str(form[k]).strip())
    return RedirectResponse("/settings?msg=" + quote("설정이 저장되었습니다"), status_code=303)


@app.post("/settings/codes")
async def code_add(request: Request):
    f = await request.form()
    if f.get("name"):
        hr_repo.add_code(f["group_code"], f["name"].strip())
    return RedirectResponse("/settings?msg=" + quote("코드가 추가되었습니다") + "#codes", status_code=303)


@app.post("/settings/codes/{cid}/delete")
def code_delete(cid: int):
    hr_repo.delete_code(cid)
    return RedirectResponse("/settings?msg=" + quote("코드가 삭제되었습니다") + "#codes", status_code=303)
