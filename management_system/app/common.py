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
                  "font": {"size", "color", "face"}}

# 계약서·증명서 글꼴. 화면은 PC 의 글꼴(맑은 고딕 등), 서버 PDF 는 같은 계열의 서버 글꼴(Noto)로 그려집니다.
DOC_FONTS = {
    "맑은 고딕": "'Malgun Gothic','맑은 고딕','Noto Sans CJK KR','Noto Sans KR',sans-serif",
    "바탕": "'Batang','바탕','Noto Serif CJK KR','Nanum Myeongjo',serif",
    "굴림": "'Gulim','굴림','Noto Sans CJK KR',sans-serif",
    "돋움": "'Dotum','돋움','Noto Sans CJK KR',sans-serif",
    "나눔고딕": "'NanumGothic','나눔고딕','Noto Sans CJK KR',sans-serif",
}


def doc_font_css(name: str | None = None) -> str:
    """문서 글꼴 CSS (설정값 → 글꼴 목록). 알 수 없는 값이면 맑은 고딕."""
    if name is None:   # 글꼴 값만 읽음 (다른 설정은 암호화돼 있어 키 없이 읽으면 실패). 못 읽으면 기본 글꼴
        try:
            from app.db import query_one
            row = query_one("SELECT value FROM settings WHERE key='doc_font'")
            name = row["value"] if row else None
        except Exception:
            name = None
    return f".paper{{font-family:{DOC_FONTS.get(name or '', DOC_FONTS['맑은 고딕'])}}}"


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


def vat_split(amount, vat_type) -> dict:
    """공급가액·부가세·합계. 입력값이 부가세포함인지 별도인지에 따라 나눕니다."""
    a = int(amount or 0)
    if vat_type == "부가세포함":
        supply = round(a / 1.1)
        return {"supply": supply, "vat": a - supply, "total": a}
    if vat_type == "부가세별도":
        vat = round(a * 0.1)
        return {"supply": a, "vat": vat, "total": a + vat}
    return {"supply": a, "vat": 0, "total": a}


def _vatinfo(amount, vat_type) -> str:
    if not amount or vat_type not in ("부가세포함", "부가세별도"):
        return vat_type or ""
    v = vat_split(amount, vat_type)
    return f"{vat_type} · 공급가 {won(v['supply'])} + 부가세 {won(v['vat'])} = 합계 {won(v['total'])}"


templates.env.filters["won"] = won
templates.env.filters["vatinfo"] = _vatinfo
templates.env.filters["man_won"] = man_won
templates.env.filters["pct"] = pct
templates.env.filters["fdate"] = _fmt_date
templates.env.filters["service"] = _service
templates.env.filters["dday"] = days_until
templates.env.globals["today"] = today
templates.env.globals["timedelta"] = timedelta
templates.env.globals["now"] = datetime.now
templates.env.globals["doc_font_css"] = doc_font_css
templates.env.globals["DOC_FONTS"] = DOC_FONTS
