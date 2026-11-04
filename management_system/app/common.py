"""공통 유틸리티: 템플릿 엔진, 포맷 필터, 날짜 계산, 금액 한글 변환."""
import os
import re
from datetime import date, datetime, timedelta

from fastapi.templating import Jinja2Templates

from app.db import BASE_DIR

templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


# ── 날짜 ────────────────────────────────────────────────────────────────
def today() -> date:
    override = os.environ.get("MGMT_TODAY")  # 테스트용 기준일 고정
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
_DANGEROUS_TAGS = re.compile(r"<\s*(script|iframe|object|embed|style|link|meta)[^>]*>.*?<\s*/\s*\1\s*>|<\s*(script|iframe|object|embed|link|meta)[^>]*/?>",
                             re.IGNORECASE | re.DOTALL)
_EVENT_ATTRS = re.compile(r"\s+on[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE)
_JS_URLS = re.compile(r"(href|src)\s*=\s*([\"'])\s*javascript:[^\"']*\2", re.IGNORECASE)


def sanitize_html(html: str) -> str:
    html = _DANGEROUS_TAGS.sub("", html or "")
    html = _EVENT_ATTRS.sub("", html)
    html = _JS_URLS.sub(r'\1="#"', html)
    return html


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
