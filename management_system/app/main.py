"""BMS 경영관리업무시스템 FastAPI 애플리케이션."""
import logging
import os
from contextlib import asynccontextmanager
from logging.handlers import TimedRotatingFileHandler
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.common import templates
from app.config import APP_DIR, SYSTEM_CODE, SYSTEM_NAME, settings
from app.db import init_db, get_settings, set_setting, DEFAULT_SETTINGS
from app.modules import MODULES
from app.modules.hr import repo as hr_repo
from app.modules.hr.routes import router as hr_router
from app.security import auth
from app.security.middleware import SecurityMiddleware
from app.security.routes import router as auth_router

log = logging.getLogger("bms")
INITIAL_PW_FILE = os.path.join(settings.secrets_dir, "initial_admin_password.txt")


def _setup_logging():
    os.makedirs(settings.log_dir, exist_ok=True)
    h = TimedRotatingFileHandler(os.path.join(settings.log_dir, "bms.log"), when="midnight", backupCount=400,
                                 encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    log.addHandler(h)
    log.setLevel(logging.INFO)


def _bootstrap_admin():
    """사용자가 한 명도 없으면 초기 관리자(admin) 생성 + 임시 비밀번호를 콘솔·비밀폴더 파일로 1회 안내."""
    if auth.count_users():
        return
    pw = os.environ.get("BMS_INITIAL_ADMIN_PASSWORD") or auth.generate_temp_password()
    auth.create_user("admin", "시스템관리자", "admin", pw, must_change=True)
    os.makedirs(settings.secrets_dir, exist_ok=True)
    with open(INITIAL_PW_FILE, "w", encoding="utf-8") as f:
        f.write(f"BMS 초기 관리자 계정\n아이디: admin\n임시 비밀번호: {pw}\n"
                "최초 로그인 시 비밀번호 변경·2단계 인증 등록 후 이 파일은 자동 삭제됩니다.\n")
    print(f"\n  👤 초기 관리자 계정 생성: admin / {pw}")
    print(f"     (같은 내용이 {INITIAL_PW_FILE} 에 저장됨. 최초 로그인 후 자동 삭제)\n")


@asynccontextmanager
async def lifespan(_app):
    from app.seed import ensure_templates, seed_sample
    _setup_logging()
    is_new = init_db()
    ensure_templates()
    if is_new and settings.seed_sample:
        seed_sample()
    _bootstrap_admin()
    auth.purge_expired()
    log.info("BMS started on port %s", settings.port)
    yield


app = FastAPI(title=SYSTEM_NAME, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SecurityMiddleware)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
app.mount("/static", StaticFiles(directory=os.path.join(APP_DIR, "static")), name="static")
app.include_router(auth_router)
app.include_router(hr_router)

templates.env.globals["MODULES"] = MODULES
templates.env.globals["SYSTEM_CODE"] = SYSTEM_CODE
templates.env.globals["company"] = lambda: get_settings().get("company_name", "")
templates.env.globals["ROLE_NAMES"] = auth.ROLES


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    if request.url.path.startswith("/hr/api/") or request.url.path.startswith("/static/"):
        from fastapi.responses import JSONResponse
        return JSONResponse({"ok": False, "message": str(exc.detail)}, status_code=exc.status_code)
    return templates.TemplateResponse(request, "error.html", {"status": exc.status_code, "message": exc.detail},
                                      status_code=exc.status_code)


@app.exception_handler(Exception)
async def server_error(request: Request, exc: Exception):
    log.exception("Unhandled error on %s", request.url.path)  # 상세 내용은 서버 로그에만 기록
    return templates.TemplateResponse(request, "error.html",
                                      {"status": 500, "message": "처리 중 오류가 발생했습니다. 관리자에게 문의하세요."},
                                      status_code=500)


@app.get("/", include_in_schema=False)
def root(request: Request):
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
        "active_menu": "settings", "msg": request.query_params.get("msg", ""),
        "groups": {"DEPT": "부서", "RANK": "직급", "TITLE": "직책", "LOCATION": "근무지"},
        "cfg": {"포트": settings.port, "HTTPS": "사용" if settings.https else "미사용(내부 테스트용)",
                "2단계 인증": "필수" if settings.require_2fa else "선택",
                "세션 유휴 만료": f"{settings.session_idle_minutes}분", "문서 보관 위치": settings.output_root,
                "DB 위치": settings.db_path, "비밀키 폴더": settings.secrets_dir},
    })


@app.post("/settings")
async def settings_save(request: Request):
    from app.db import audit
    form = await request.form()
    for k in DEFAULT_SETTINGS:
        if k in form:
            set_setting(k, str(form[k]).strip())
    audit("system", "설정 변경", "회사정보", "")
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
