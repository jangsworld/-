"""공통 유틸리티: 템플릿 엔진, 포맷 필터, 날짜 계산, 금액 한글 변환."""
import os
from datetime import date, datetime, timedelta

import nh3
from fastapi.templating import Jinja2Templates

from app.config import APP_DIR as BASE_DIR, settings


def _security_context(request) -> dict:
    """모든 화면 공통: 로그인 사용자, 권한, CSRF 토큰, CSP nonce."""
    user = getattr(request.state, "user", None)
    sess = getattr(request.state, "session", None)
    role = user["role"] if user else None
    return {
        "current_user": user,
        "csrf_token": sess["csrf_token"] if sess else "",
        "csp_nonce": getattr(request.state, "csp_nonce", ""),
        "idle_minutes": settings.session_idle_minutes,
        "perms": {
            "admin": role == "admin",
            "write": role in ("admin", "hr"),
            "salary": role in ("admin", "hr"),   # 연봉·계약서 열람
            "pii": role in ("admin", "hr"),      # 연락처·주소·계좌 등 개인정보 열람
            "ga_write": role in ("admin", "hr", "ga"),
            "ga_events": role in ("admin", "hr", "ga"),
        },
    }


templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"), context_processors=[_security_context])


# ── 날짜 ────────────────────────────────────────────────────────────────
def today() -> date:
    override = os.environ.get("BMS_TODAY")  # 테스트용 기준일 고정
    return date.fromisoformat(override) if override else date.today()


def to_date(v) -> date | None:
    if not v:
        return None
    if isinstance(v, date):
        return v
    s = str(v).strip().replace(".", "-").replace("/", "-")[:10]
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def add_years(d: date, n: int) -> date:
    try:
        return d.replace(year=d.year + n)
    except ValueError:  # 2/29
        return d.replace(year=d.year + n, day=28)


def add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    import calendar
    last = calendar.monthrange(y, m)[1]
    return date(y, m, min(d.day, last))


def service_period(start, end=None) -> tuple[int, int]:
    """근속기간 (년, 개월)."""
    s, e = to_date(start), to_date(end) or today()
    if not s or e < s:
        return 0, 0
    months = (e.year - s.year) * 12 + (e.month - s.month) - (1 if e.day < s.day else 0)
    return months // 12, months % 12


def service_years_float(start, end=None) -> float:
    s, e = to_date(start), to_date(end) or today()
    if not s or e < s:
        return 0.0
    return (e - s).days / 365.25


def days_until(v) -> int | None:
    d = to_date(v)
    return (d - today()).days if d else None


# ── 금액 ────────────────────────────────────────────────────────────────
def won(v) -> str:
    if v is None or v == "":
        return "-"
    try:
        return f"{int(round(float(v))):,}"
    except (TypeError, ValueError):
        return str(v)


def man_won(v) -> str:
    """만원 단위 표시."""
    if not v:
        return "-"
    return f"{float(v) / 10000:,.0f}만"


_DIGITS = "영일이삼사오육칠팔구"
_SMALL = ["", "십", "백", "천"]
_LARGE = ["", "만", "억", "조"]


def korean_amount(n) -> str:
    """정수 금액 → 한글 (예: 36000000 → 삼천육백만)."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    if n == 0:
        return "영"
    out = []
    group = 0
    while n > 0:
        chunk = n % 10000
        if chunk:
            s = ""
            for i in range(4):
                d = (chunk // (10 ** i)) % 10
                if d:
                    s = (_DIGITS[d] if not (d == 1 and i > 0) else "") + _SMALL[i] + s
            out.insert(0, s + _LARGE[group])
        n //= 10000
        group += 1
    return "".join(out)


def pct(v, digits=1) -> str:
    if v is None:
        return "-"
    return f"{v:+.{digits}f}%"


# ── HTML 정리 (에디터 저장 내용) ─────────────────────────────────────────
_ALLOWED_TAGS = {"p", "br", "b", "strong", "i", "em", "u", "s", "strike", "span", "div", "font", "h1", "h2", "h3",
                 "h4", "ul", "ol", "li", "table", "thead", "tbody", "tr", "th", "td", "hr", "sub", "sup", "blockquote"}
_ALLOWED_ATTRS = {"*": {"class", "style", "align"}, "td": {"colspan", "rowspan"}, "th": {"colspan", "rowspan"},
                  "font": {"size", "color"}}


def sanitize_html(html: str) -> str:
    """허용목록 기반 HTML 정화(nh3). 스크립트·이벤트 속성·링크·이미지 등은 모두 제거."""
    return nh3.clean(html or "", tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRS, link_rel=None,
                     strip_comments=True)


# ── Jinja 필터 등록 ─────────────────────────────────────────────────────
def _fmt_date(v, fmt="%Y-%m-%d"):
    d = to_date(v)
    return d.strftime(fmt) if d else "-"


def _service(v, end=None):
    y, m = service_period(v, end)
    return f"{y}년 {m}개월"


templates.env.filters["won"] = won
templates.env.filters["man_won"] = man_won
templates.env.filters["pct"] = pct
templates.env.filters["fdate"] = _fmt_date
templates.env.filters["service"] = _service
templates.env.filters["dday"] = days_until
templates.env.globals["today"] = today
templates.env.globals["timedelta"] = timedelta
templates.env.globals["now"] = datetime.now
