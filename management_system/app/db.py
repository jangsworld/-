"""SQLite 연결 및 스키마 관리 (경영관리시스템 공통)."""
import contextvars
import os
import sqlite3
from contextlib import contextmanager

from app.config import APP_DIR as BASE_DIR, settings
from app.security.crypto import dec_row

DB_PATH = settings.db_path
SCHEMA_VERSION = "2"

# 요청 단위 사용자·IP (감사로그 기록용, 미들웨어에서 설정)
current_actor: contextvars.ContextVar[tuple] = contextvars.ContextVar("bms_actor", default=(None, "system", ""))


def get_connection() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def tx():
    """트랜잭션 컨텍스트: 정상 종료 시 commit, 예외 시 rollback."""
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def query(sql: str, params=()) -> list[dict]:
    """SELECT 결과를 dict 로 반환. 암호화 필드(enc:v1:)는 자동 복호화."""
    with tx() as conn:
        return [dec_row(dict(r)) for r in conn.execute(sql, params).fetchall()]


def query_one(sql: str, params=()) -> dict | None:
    with tx() as conn:
        row = conn.execute(sql, params).fetchone()
        return dec_row(dict(row)) if row else None


def execute(sql: str, params=()) -> int:
    """INSERT/UPDATE/DELETE 실행 후 lastrowid 반환."""
    with tx() as conn:
        cur = conn.execute(sql, params)
        return cur.lastrowid


SCHEMA = """
-- ── 공통 ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS codes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    group_code TEXT NOT NULL,          -- DEPT / RANK / TITLE / LOCATION
    name       TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    UNIQUE(group_code, name)
);

-- 개인정보 접속기록 겸 감사로그 (개인정보 안전성 확보조치 기준: 1년 이상 보관·월 1회 점검)
CREATE TABLE IF NOT EXISTS audit_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    username   TEXT,
    ip         TEXT,
    module     TEXT NOT NULL,
    action     TEXT NOT NULL,
    target     TEXT,
    detail     TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- ── 보안: 사용자 / 세션 / 로그인 시도 ──────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    username            TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name        TEXT NOT NULL,
    role                TEXT NOT NULL CHECK(role IN ('admin','hr','viewer')),
    password_hash       TEXT NOT NULL,
    must_change_pw      INTEGER NOT NULL DEFAULT 1,
    pw_changed_at       TEXT,
    totp_secret         TEXT,                 -- 암호화 저장
    totp_enabled        INTEGER NOT NULL DEFAULT 0,
    totp_last_step      INTEGER,              -- OTP 재사용 방지
    failed_count        INTEGER NOT NULL DEFAULT 0,
    locked_until        TEXT,
    is_active           INTEGER NOT NULL DEFAULT 1,
    last_login_at       TEXT,
    last_login_ip       TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash   TEXT PRIMARY KEY,            -- 쿠키 토큰의 SHA-256 (원문은 저장하지 않음)
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf_token   TEXT NOT NULL,
    mfa_passed   INTEGER NOT NULL DEFAULT 0,
    ip           TEXT,
    user_agent   TEXT,
    created_at   TEXT NOT NULL,
    last_seen    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS login_attempts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ip         TEXT,
    username   TEXT,
    success    INTEGER NOT NULL,
    reason     TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- ── 인사: 임직원 ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS employees (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    emp_no            TEXT NOT NULL UNIQUE,
    name              TEXT NOT NULL,
    birth_date        TEXT,
    gender            TEXT,
    phone             TEXT,
    email             TEXT,
    address           TEXT,
    emergency_contact TEXT,
    department        TEXT,
    rank              TEXT,               -- 직급
    title             TEXT,               -- 직책
    job_duty          TEXT,               -- 담당업무
    employment_type   TEXT NOT NULL DEFAULT '정규직',
    work_location     TEXT,
    hire_date         TEXT NOT NULL,
    probation_end     TEXT,               -- 수습 종료일
    resign_date       TEXT,
    resign_reason     TEXT,
    bank_name         TEXT,
    bank_account      TEXT,
    education         TEXT,
    certificates      TEXT,               -- 보유 자격
    memo              TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- ── 인사: 발령 이력 ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS appointments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    appt_date   TEXT NOT NULL,
    appt_type   TEXT NOT NULL,             -- 입사/승진/전보/직책변경/고용형태변경/퇴사 등
    from_value  TEXT,
    to_value    TEXT,
    note        TEXT
);

-- ── 인사: 문서 템플릿 (근로계약서 / 증명서) ─────────────────────────
CREATE TABLE IF NOT EXISTS doc_templates (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_group  TEXT NOT NULL DEFAULT 'contract',   -- contract / certificate
    category   TEXT NOT NULL,                      -- 정규직/기간제/단시간/연봉계약/재직증명/경력증명
    name       TEXT NOT NULL,
    content    TEXT NOT NULL,
    is_active  INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- ── 인사: 작성 문서 (근로계약서 / 증명서 발급) ──────────────────────
CREATE TABLE IF NOT EXISTS hr_documents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_group     TEXT NOT NULL DEFAULT 'contract',
    employee_id   INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    template_id   INTEGER REFERENCES doc_templates(id) ON DELETE SET NULL,
    category      TEXT NOT NULL,
    title         TEXT NOT NULL,
    doc_no        TEXT,                    -- 증명서 발급번호
    purpose       TEXT,                    -- 증명서 용도
    start_date    TEXT,
    end_date      TEXT,
    annual_salary TEXT,                    -- 암호화
    weekly_hours  REAL,
    params        TEXT,                    -- 작성 시 입력값(JSON, 암호화)
    content       TEXT NOT NULL,           -- 문서 본문(HTML, 암호화)
    pdf_path      TEXT,                    -- NAS 보관 PDF 경로
    pdf_sha256    TEXT,                    -- 위·변조 확인용 해시
    pdf_saved_at  TEXT,
    status        TEXT NOT NULL DEFAULT '작성중',  -- 작성중/서명대기/체결완료/만료/해지/발급완료
    signed_date   TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- ── 인사: 연봉 이력 ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS salaries (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id    INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    year           INTEGER NOT NULL,
    effective_date TEXT,
    annual_salary  TEXT NOT NULL,          -- 암호화
    reason         TEXT DEFAULT '정기인상',   -- 신규/정기인상/승진/특별인상/조정/동결
    note           TEXT,
    UNIQUE(employee_id, year)
);

-- ── 인사: 상벌 ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS rewards (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id   INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL CHECK(kind IN ('포상','징계')),
    category      TEXT NOT NULL,
    action_date   TEXT NOT NULL,
    reason        TEXT,
    amount        TEXT,                    -- 암호화
    period_start  TEXT,
    period_end    TEXT,
    expunge_date  TEXT,                    -- 징계 말소(효력 종료) 예정일
    issuer        TEXT,
    note          TEXT
);

-- ── 인사: 근로특이사항 (휴직·단축근무·모성보호 등) ──────────────────
CREATE TABLE IF NOT EXISTS work_specials (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id    INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    category       TEXT NOT NULL,
    start_date     TEXT NOT NULL,
    end_date       TEXT,
    actual_end     TEXT,                   -- 실제 종료/복직일 (조기복귀 등)
    reduced_hours  REAL,                   -- 단축 후 주 소정근로시간
    child_name     TEXT,
    child_birth    TEXT,
    replacement    TEXT,                   -- 대체인력
    subsidy        TEXT,                   -- 정부지원금 신청 현황
    note           TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_login_attempts ON login_attempts(ip, created_at);
CREATE INDEX IF NOT EXISTS idx_salaries_year ON salaries(year);
CREATE INDEX IF NOT EXISTS idx_docs_emp ON hr_documents(employee_id);
CREATE INDEX IF NOT EXISTS idx_rewards_emp ON rewards(employee_id);
CREATE INDEX IF NOT EXISTS idx_specials_emp ON work_specials(employee_id);
"""

DEFAULT_SETTINGS = {
    "company_name": "(주)샘플컴퍼니",
    "ceo_name": "홍길동",
    "company_address": "서울특별시 중구 세종대로 110",
    "business_no": "123-45-67890",
    "company_phone": "02-000-0000",
    "pay_day": "25",
    "min_wage_hourly": "10320",      # 2026년 최저임금(시간급)
    "monthly_std_hours": "209",       # 주 40시간 기준 월 소정근로시간(유급주휴 포함)
    "alert_days": "60",               # 만료·복직 등 사전 알림 기간(일)
}

DEFAULT_CODES = {
    "DEPT": ["경영지원팀", "인사팀", "재무팀", "영업팀", "기술팀", "연구소"],
    "RANK": ["사원", "주임", "대리", "과장", "차장", "부장", "이사", "상무", "대표이사"],
    "TITLE": ["팀원", "파트장", "팀장", "본부장", "연구소장", "대표"],
    "LOCATION": ["본사", "지사", "현장"],
}


def init_db() -> bool:
    """마스터키 로드 + 스키마 생성 + 기본값 삽입. 최초 생성 여부를 반환."""
    from app.security.crypto import load_or_create_key
    is_new = not os.path.exists(DB_PATH)
    load_or_create_key(allow_create=is_new)
    with tx() as conn:
        if not is_new:
            has_users = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
            if not has_users:
                raise RuntimeError("[BMS] 보안 기능 이전 버전의 DB 입니다. 샘플 DB 라면 삭제 후 다시 시작하세요: " + DB_PATH)
        conn.executescript(SCHEMA)
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
        has_codes = conn.execute("SELECT COUNT(*) FROM codes").fetchone()[0]
        if not has_codes:
            for group, names in DEFAULT_CODES.items():
                for i, n in enumerate(names):
                    conn.execute(
                        "INSERT INTO codes(group_code, name, sort_order) VALUES (?,?,?)",
                        (group, n, i),
                    )
    return is_new


def get_settings() -> dict:
    return {r["key"]: r["value"] for r in query("SELECT key, value FROM settings")}


def set_setting(key: str, value: str):
    execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def get_codes(group: str) -> list[str]:
    return [r["name"] for r in query(
        "SELECT name FROM codes WHERE group_code=? ORDER BY sort_order, id", (group,)
    )]


def audit(module: str, action: str, target: str = "", detail: str = ""):
    """감사로그 기록. 개인정보 원문(연봉·연락처 등)은 detail 에 넣지 않습니다."""
    uid, uname, ip = current_actor.get()
    execute(
        "INSERT INTO audit_logs(user_id, username, ip, module, action, target, detail) VALUES (?,?,?,?,?,?,?)",
        (uid, uname, ip, module, action, str(target or ""), str(detail or "")),
    )
