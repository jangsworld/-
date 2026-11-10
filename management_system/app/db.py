"""SQLite 연결 및 스키마 관리 (경영관리시스템 공통)."""
import contextvars
import os
import sqlite3
import threading
from contextlib import contextmanager

from app.config import APP_DIR as BASE_DIR, settings
from app.security.crypto import dec_row

DB_PATH = settings.db_path
SCHEMA_VERSION = "6"

# 요청 단위 사용자·IP (감사로그 기록용, 미들웨어에서 설정)
current_actor: contextvars.ContextVar[tuple] = contextvars.ContextVar("bms_actor", default=(None, "system", ""))


# 다른 쪽이 쓰는 중이면 실패하지 않고 기다리는 시간(초). 기본 5초는 대량 저장 중 화면 오류(database is locked)를 냄
BUSY_TIMEOUT_SEC = 30

# 같은 스레드에서 batch() 안에 있으면 query/execute 가 그 트랜잭션 하나를 함께 씁니다
_local = threading.local()


def get_connection() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=BUSY_TIMEOUT_SEC)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def tx():
    """트랜잭션 컨텍스트: 정상 종료 시 commit, 예외 시 rollback.
    batch() 안에서 부르면 바깥 트랜잭션에 합류합니다(커밋은 batch 가 끝날 때 한 번)."""
    shared = getattr(_local, "conn", None)
    if shared is not None:
        yield shared
        return
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def batch():
    """여러 건 저장을 한 번에 커밋합니다(대량 동기화용). 건마다 커밋하면 그동안 다른 화면이 DB 를 못 읽습니다."""
    if getattr(_local, "conn", None) is not None:      # 이미 batch 안
        yield _local.conn
        return
    with tx() as conn:
        _local.conn = conn
        try:
            yield conn
        finally:
            _local.conn = None


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
    behavior   TEXT,                   -- ASSET_EVENT: 처리 시 자산 상태 변경 방식
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
    role                TEXT NOT NULL CHECK(role IN ('admin','hr','ga','viewer')),
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
    emergency_relation TEXT,              -- 비상연락처 관계 (배우자, 아들 등)
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
    education         TEXT,               -- 최종학력 요약 (edu_* 로 자동 작성)
    edu_level         TEXT,               -- 박사/석사/학사/전문학사/고졸
    edu_school        TEXT,
    edu_major         TEXT,
    edu_grad_year     TEXT,
    certificates      TEXT,               -- 기타 보유 자격
    folder_name       TEXT,               -- 임직원 서류 폴더명 (employee_root 하위)
    memo              TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 주민등록번호: 일반 조회(SELECT * FROM employees)에 섞이지 않도록 별도 테이블에 암호화 저장
CREATE TABLE IF NOT EXISTS employee_rrn (
    employee_id INTEGER PRIMARY KEY REFERENCES employees(id) ON DELETE CASCADE,
    rrn         TEXT NOT NULL,            -- 암호화
    updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 보유자격: 정보통신공사기술자 / 전기공사기술자 / 소프트웨어경력증명서
CREATE TABLE IF NOT EXISTS employee_qualifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    license_no  TEXT,                     -- 암호화
    grade       TEXT,
    main_cert   TEXT,                     -- 대표 국가기술자격증
    UNIQUE(employee_id, kind)
);

-- 임직원 서류 폴더에서 자동 생성된 등록 초안 (담당자 확인 전에는 employees 에 넣지 않음)
CREATE TABLE IF NOT EXISTS employee_drafts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    folder_name TEXT NOT NULL UNIQUE,
    status      TEXT NOT NULL DEFAULT '확인필요',  -- 확인필요/등록완료/제외
    signature   TEXT,                     -- 폴더 내용 서명 (서류 추가 시 초안 갱신)
    data        TEXT,                     -- 추출 항목(JSON, 암호화). OCR 원문은 저장하지 않음
    employee_id INTEGER REFERENCES employees(id) ON DELETE SET NULL,
    detected_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
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
    emailed_at    TEXT,                    -- 직원에게 메일 전송한 일시
    status        TEXT NOT NULL DEFAULT '작성중',  -- 작성중/서명대기/체결완료/만료/해지/발급완료
    signed_date   TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 문서(증명서 등) 메일 발송 기록: 누구에게(인사기록카드/직접 입력) 언제 보냈는지
CREATE TABLE IF NOT EXISTS hr_doc_mails (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id      INTEGER NOT NULL REFERENCES hr_documents(id) ON DELETE CASCADE,
    target      TEXT NOT NULL,            -- 인사기록카드 / 직접입력
    to_addr     TEXT NOT NULL,            -- 암호화
    sent_by     TEXT,
    sent_at     TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- ── 인사: 연차 (메일플러그 휴가 내역 엑셀 → 현황·촉진) ──────────────
-- 메일플러그 휴가 문서 (연차 종류만 보관, 사유·보건휴가는 저장하지 않음)
CREATE TABLE IF NOT EXISTS leave_records (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_no      TEXT NOT NULL UNIQUE,     -- 메일플러그 문서번호
    employee_id INTEGER REFERENCES employees(id) ON DELETE SET NULL,
    mp_name     TEXT NOT NULL,            -- 메일플러그 이름 (직원 연결 전 표시용)
    mp_dept     TEXT,
    mp_rank     TEXT,
    leave_type  TEXT NOT NULL,
    start_date  TEXT NOT NULL,
    end_date    TEXT NOT NULL,
    time_text   TEXT,                     -- 1d (8h) / 오후 (4h)
    days        REAL NOT NULL,
    status      TEXT NOT NULL,
    import_id   INTEGER,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS leave_imports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    filename    TEXT NOT NULL,
    period_from TEXT NOT NULL,
    period_to   TEXT NOT NULL,
    total       INTEGER, added INTEGER, changed INTEGER, removed INTEGER, skipped INTEGER,
    imported_by TEXT,
    imported_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
-- 메일플러그 이름 ↔ 임직원 (동명이인·영문 이름 등 담당자가 한 번 연결)
CREATE TABLE IF NOT EXISTS leave_name_map (
    mp_name     TEXT PRIMARY KEY,
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE
);
-- 직원·연도별 조정 (올리기 전 사용분, 이월, 메일플러그와 차이 등)
CREATE TABLE IF NOT EXISTS leave_adjust (
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    year        INTEGER NOT NULL,
    days        REAL NOT NULL DEFAULT 0,  -- 발생 가감 (+이월, -조정)
    used_before REAL NOT NULL DEFAULT 0,  -- 엑셀로 올리지 않은 기간의 사용일수
    note        TEXT,
    PRIMARY KEY (employee_id, year)
);
-- 연차 사용 촉진 (근로기준법 제61조): 1차 = 잔여 통보 + 사용시기 회신 요청, 2차 = 회사 지정 통보
CREATE TABLE IF NOT EXISTS leave_promotions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id     INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    year            INTEGER NOT NULL,
    round           INTEGER NOT NULL,      -- 1 / 2
    remaining       REAL NOT NULL,
    deadline        TEXT,                  -- 1차: 회신 기한
    to_addr         TEXT,                  -- 암호화
    mailed          INTEGER NOT NULL DEFAULT 0,
    token_hash      TEXT UNIQUE,
    token           TEXT,                  -- 암호화 (담당자 재안내용)
    fail_count      INTEGER NOT NULL DEFAULT 0,
    session_hash    TEXT,
    session_expires TEXT,
    opened_at       TEXT,
    responded_at    TEXT,
    responder_ip    TEXT,
    status          TEXT NOT NULL,         -- 회신대기/회신완료/취소/지정통보
    doc_id          INTEGER REFERENCES hr_documents(id) ON DELETE SET NULL,
    sent_by         TEXT,
    sent_at         TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
-- 사용 계획 (직원 회신 또는 회사 지정)
CREATE TABLE IF NOT EXISTS leave_plans (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id  INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    year         INTEGER NOT NULL,
    plan_date    TEXT NOT NULL,
    part         TEXT NOT NULL DEFAULT '종일',   -- 종일/오전/오후
    source       TEXT NOT NULL,                  -- 직원회신/회사지정
    promotion_id INTEGER REFERENCES leave_promotions(id) ON DELETE SET NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 상해공제(단체상해보험) 가입 이력: 안전사고 대비 직원별 가입
CREATE TABLE IF NOT EXISTS employee_insurances (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    product     TEXT NOT NULL,            -- 공제명(상품명)
    insurer     TEXT,                     -- 보험사·공제조합
    joined_on   TEXT,                     -- 가입일(청약일)
    start_date  TEXT,                     -- 보장 시작
    end_date    TEXT,                     -- 보장 종료(만기)
    policy_no   TEXT,                     -- 증권·가입번호
    premium     TEXT,                     -- 공제료(원)
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 입사·퇴사 체크리스트 완료 기록 (항목은 구분값 설정의 ONBOARD/OFFBOARD)
CREATE TABLE IF NOT EXISTS employee_checks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,            -- ONBOARD / OFFBOARD
    item        TEXT NOT NULL,
    done_date   TEXT NOT NULL,
    done_by     TEXT,
    UNIQUE(employee_id, kind, item)
);

-- 전자서명 요청 (직원 메일 링크)
CREATE TABLE IF NOT EXISTS hr_sign_requests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id          INTEGER NOT NULL REFERENCES hr_documents(id) ON DELETE CASCADE,
    token_hash      TEXT NOT NULL UNIQUE,
    token           TEXT,                  -- 담당자 재안내용, 암호화
    email           TEXT,                  -- 암호화
    status          TEXT NOT NULL DEFAULT '대기',  -- 대기/완료/취소
    content_sha256  TEXT NOT NULL,         -- 발송 시점 본문 해시
    expires_at      TEXT NOT NULL,
    sent_at         TEXT NOT NULL,
    mailed          INTEGER NOT NULL DEFAULT 0,
    fail_count      INTEGER NOT NULL DEFAULT 0,
    session_hash    TEXT,
    session_expires TEXT,
    opened_at       TEXT,
    signed_at       TEXT,
    signer_ip       TEXT,
    signer_ua       TEXT
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
-- ════════════════════════════════════════════════════════════════════
-- 총무(GA)
-- ════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS ga_assets (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_no       TEXT NOT NULL UNIQUE,          -- IT-2026-001
    category       TEXT NOT NULL,
    name           TEXT NOT NULL,
    model          TEXT,
    serial_no      TEXT,
    vendor         TEXT,                          -- 구입처
    acquired_date  TEXT,
    cost           INTEGER DEFAULT 0,             -- 취득가
    useful_years   INTEGER DEFAULT 5,             -- 내용연수
    location       TEXT,
    status         TEXT NOT NULL DEFAULT '사용중', -- 사용중/보관/수리중/분실/폐기
    holder_id      INTEGER REFERENCES employees(id) ON DELETE SET NULL,
    warranty_end   TEXT,                          -- 보증(AS)·라이선스 만료일
    disposed_date  TEXT,
    memo           TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS ga_asset_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id    INTEGER NOT NULL REFERENCES ga_assets(id) ON DELETE CASCADE,
    event_date  TEXT NOT NULL,
    event_type  TEXT NOT NULL,                    -- 취득/지급/반납/이동/수리/폐기/점검
    holder_id   INTEGER REFERENCES employees(id) ON DELETE SET NULL,
    cost        INTEGER DEFAULT 0,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS ga_vehicles (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    plate_no         TEXT NOT NULL UNIQUE,
    model            TEXT NOT NULL,
    model_year       INTEGER,
    fuel             TEXT,
    ownership        TEXT DEFAULT '소유',          -- 소유/리스/장기렌트
    acquired_date    TEXT,
    lease_end        TEXT,
    monthly_cost     INTEGER DEFAULT 0,            -- 리스·렌트 월 납입액
    insurer          TEXT,
    insurance_end    TEXT,
    inspection_due   TEXT,                         -- 정기검사 만료
    manager_id       INTEGER REFERENCES employees(id) ON DELETE SET NULL,
    status           TEXT NOT NULL DEFAULT '운행', -- 운행/정비중/매각
    odometer_start   INTEGER DEFAULT 0,
    memo             TEXT
);
CREATE TABLE IF NOT EXISTS ga_vehicle_logs (           -- 운행기록부
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_id   INTEGER NOT NULL REFERENCES ga_vehicles(id) ON DELETE CASCADE,
    drive_date   TEXT NOT NULL,
    driver_id    INTEGER REFERENCES employees(id) ON DELETE SET NULL,
    purpose      TEXT NOT NULL DEFAULT '일반업무',  -- 일반업무/출퇴근/비업무/확인필요
    destination  TEXT,                              -- 암호화
    km_start     INTEGER NOT NULL,
    km_end       INTEGER NOT NULL,
    fuel_cost    INTEGER DEFAULT 0,
    toll_cost    INTEGER DEFAULT 0,
    note         TEXT
);
CREATE TABLE IF NOT EXISTS ga_vehicle_maint (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_id  INTEGER NOT NULL REFERENCES ga_vehicles(id) ON DELETE CASCADE,
    maint_date  TEXT NOT NULL,
    category    TEXT NOT NULL,                     -- 정비/소모품/보험/세금/검사/사고/기타
    detail      TEXT,
    km          INTEGER,
    cost        INTEGER DEFAULT 0,
    vendor      TEXT
);

CREATE TABLE IF NOT EXISTS ga_docs (                   -- 문서 수발 대장
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    direction    TEXT NOT NULL CHECK(direction IN ('수신','발신')),
    doc_no       TEXT NOT NULL UNIQUE,
    doc_date     TEXT NOT NULL,
    counterpart  TEXT NOT NULL,                     -- 발신처/수신처
    title        TEXT NOT NULL,
    department   TEXT,
    handler_id   INTEGER REFERENCES employees(id) ON DELETE SET NULL,
    method       TEXT,                              -- 우편/등기/이메일/팩스/방문/전자문서
    status       TEXT NOT NULL DEFAULT '접수',       -- 접수/처리중/완료/회신필요
    due_date     TEXT,                              -- 회신·처리 기한
    retention    TEXT DEFAULT '5년',
    note         TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS ga_seal_logs (              -- 관인(인감) 사용 대장
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    use_date      TEXT NOT NULL,
    seal_type     TEXT NOT NULL,                     -- 법인인감/사용인감/직인/대표자 개인인감
    purpose       TEXT NOT NULL,
    submit_to     TEXT,
    copies        INTEGER DEFAULT 1,
    requester_id  INTEGER REFERENCES employees(id) ON DELETE SET NULL,
    status        TEXT NOT NULL DEFAULT '신청',       -- 신청/승인/반려/사용완료
    approver      TEXT,
    approved_at   TEXT,
    note          TEXT
);

CREATE TABLE IF NOT EXISTS ga_contracts (              -- 계약·임대차
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    category       TEXT NOT NULL,                    -- 임대차/용역/유지보수/리스·렌탈/구매/보험/기타
    title          TEXT NOT NULL,
    counterparty   TEXT NOT NULL,
    contract_no    TEXT,
    start_date     TEXT,
    end_date       TEXT,
    amount         INTEGER DEFAULT 0,
    pay_cycle      TEXT DEFAULT '월',                 -- 월/분기/반기/연/일시
    deposit        INTEGER DEFAULT 0,                 -- 보증금
    auto_renew     INTEGER DEFAULT 0,
    notice_days    INTEGER DEFAULT 30,                -- 해지·갱신 통보 기한(만료 N일 전)
    manager_id     INTEGER REFERENCES employees(id) ON DELETE SET NULL,
    status         TEXT NOT NULL DEFAULT '유효',      -- 유효/해지/종료
    memo           TEXT
);

CREATE TABLE IF NOT EXISTS ga_contract_drafts (        -- 계약서 폴더에서 읽은 등록 초안
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    folder_name TEXT NOT NULL UNIQUE,
    signature   TEXT,                                -- 폴더 파일 목록 해시 (바뀌면 다시 읽음)
    data        TEXT,                                -- 암호화: 추출 항목·파일 목록
    status      TEXT NOT NULL DEFAULT '확인필요',     -- 확인필요/등록완료/삭제/제외
    contract_id INTEGER REFERENCES ga_contracts(id) ON DELETE SET NULL,
    detected_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS ga_event_rules (            -- 경조 규정
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    category    TEXT NOT NULL,                       -- 결혼/출산/사망/회갑·칠순/입원/기타
    relation    TEXT NOT NULL,                       -- 본인/자녀/부모/배우자 부모/조부모/형제자매
    amount      INTEGER DEFAULT 0,
    leave_days  INTEGER DEFAULT 0,
    flower      INTEGER DEFAULT 0,                   -- 화환·조화 지원
    UNIQUE(category, relation)
);
CREATE TABLE IF NOT EXISTS ga_events (                 -- 경조사 내역
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id  INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    category     TEXT NOT NULL,
    relation     TEXT NOT NULL,
    event_date   TEXT NOT NULL,
    amount       INTEGER DEFAULT 0,
    leave_days   INTEGER DEFAULT 0,
    flower       INTEGER DEFAULT 0,
    paid_date    TEXT,
    status       TEXT NOT NULL DEFAULT '신청',        -- 신청/지급완료/취소
    detail       TEXT,                               -- 암호화 (장소·대상자 등)
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_vlogs ON ga_vehicle_logs(vehicle_id, drive_date);
CREATE INDEX IF NOT EXISTS idx_ga_docs ON ga_docs(direction, doc_date);
CREATE INDEX IF NOT EXISTS idx_salaries_year ON salaries(year);
CREATE INDEX IF NOT EXISTS idx_docs_emp ON hr_documents(employee_id);
CREATE INDEX IF NOT EXISTS idx_doc_mails ON hr_doc_mails(doc_id);
CREATE INDEX IF NOT EXISTS idx_leave_emp ON leave_records(employee_id, start_date);
CREATE INDEX IF NOT EXISTS idx_leave_promo ON leave_promotions(employee_id, year);
CREATE INDEX IF NOT EXISTS idx_leave_plans ON leave_plans(employee_id, year);
CREATE INDEX IF NOT EXISTS idx_rewards_emp ON rewards(employee_id);
CREATE INDEX IF NOT EXISTS idx_specials_emp ON work_specials(employee_id);
CREATE INDEX IF NOT EXISTS idx_ins_emp ON employee_insurances(employee_id);
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
    "doc_prefix": "총무",             # 발신 문서번호 접두어 (예: 총무-2026-0001)
    "doc_font": "맑은 고딕",          # 계약서·증명서 기본 글꼴 (app/common.py DOC_FONTS)
}

ASSET_EVENT_BEHAVIOR = {"지급": "지급", "반납": "반납", "수리": "수리", "수리완료": "수리완료", "폐기": "폐기"}

DEFAULT_CODES = {
    "DEPT": ["경영지원팀", "인사팀", "재무팀", "영업팀", "기술팀", "연구소"],
    "RANK": ["사원", "주임", "대리", "과장", "차장", "부장", "이사", "상무", "대표이사"],
    "TITLE": ["팀원", "파트장", "팀장", "본부장", "연구소장", "대표"],
    "LOCATION": ["본사", "지사", "현장"],
    "ASSET_LOC": ["본사 3층 사무실", "본사 4층 회의실", "본사 창고", "지사 사무실"],
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
            _migrate_users_role(conn)
        conn.executescript(SCHEMA)
        _add_columns(conn)
        conn.execute("INSERT INTO settings(key, value) VALUES ('schema_version', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (SCHEMA_VERSION,))
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
        for group, names in _all_default_codes().items():
            if conn.execute("SELECT COUNT(*) FROM codes WHERE group_code=?", (group,)).fetchone()[0]:
                continue
            for i, n in enumerate(names):
                conn.execute("INSERT INTO codes(group_code, name, sort_order, behavior) VALUES (?,?,?,?)",
                             (group, n, i, ASSET_EVENT_BEHAVIOR.get(n) if group == "ASSET_EVENT" else None))
    return is_new


def _all_default_codes() -> dict:
    """구분값 기본 목록. 설정 화면에서 바꾼 값은 그대로 유지됩니다(비어 있는 그룹만 채움)."""
    from app.modules.ga import constants as ga
    from app.modules.hr import constants as hr
    return {
        **DEFAULT_CODES,
        "EMPLOYMENT_TYPE": hr.EMPLOYMENT_TYPES, "EMERGENCY_RELATION": hr.EMERGENCY_RELATIONS, "EDU_LEVEL": hr.EDU_LEVELS,
        "APPOINTMENT_TYPE": hr.APPOINTMENT_TYPES, "SALARY_REASON": hr.SALARY_REASONS,
        "REWARD_포상": hr.REWARD_CATEGORIES["포상"], "REWARD_징계": hr.REWARD_CATEGORIES["징계"],
        "QUAL_GRADE": hr.QUALIFICATION_GRADES, "INTAKE_DOC": hr.INTAKE_DOCS, "ONBOARD": hr.ONBOARD_ITEMS,
        "OFFBOARD": hr.OFFBOARD_ITEMS,
        "ASSET_EVENT": ga.ASSET_EVENTS, "FUEL": ga.FUEL_TYPES, "VEHICLE_OWNERSHIP": ga.VEHICLE_OWNERSHIP,
        "MAINT_CATEGORY": ga.MAINT_CATEGORIES, "DOC_METHOD": ga.DOC_METHODS, "RETENTION": ga.RETENTION,
        "SEAL_TYPE": ga.SEAL_TYPES, "GA_CONTRACT": ga.CONTRACT_CATEGORIES, "EVENT_CATEGORY": ga.EVENT_CATEGORIES,
        "EVENT_RELATION": ga.EVENT_RELATIONS,
    }


# v3→v4: 기존 DB 에 없는 컬럼 추가 (CREATE TABLE IF NOT EXISTS 는 기존 테이블을 바꾸지 않음)
ADDED_COLUMNS = {
    "employees": ["emergency_relation", "edu_level", "edu_school", "edu_major", "edu_grad_year", "folder_name"],
    "hr_documents": ["emailed_at"],
    "codes": ["behavior"],
    "employee_qualifications": ["expires_on"],
    "ga_assets": ["cost_vat", "photo", "qr_token"],
    "ga_asset_history": ["cost_vat"],
    "ga_vehicle_maint": ["cost_vat"],
    "ga_contracts": ["amount_vat", "folder_name"],
    "ga_vehicle_logs": ["external_id", "external_updated", "external_purpose"],
}


def _add_columns(conn):
    for table, cols in ADDED_COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col in cols:
            if have and col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
    if "qr_token" in {r[1] for r in conn.execute("PRAGMA table_info(ga_assets)")}:
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_asset_qr ON ga_assets(qr_token)")
    if "external_id" in {r[1] for r in conn.execute("PRAGMA table_info(ga_vehicle_logs)")}:
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_vlog_external ON ga_vehicle_logs(external_id)")


def _migrate_users_role(conn):
    """v2→v3: users.role 에 'ga'(총무담당) 허용. SQLite 는 CHECK 변경이 안 되므로 테이블 재생성."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
    if not row or "'ga'" in row[0]:
        return
    conn.execute("PRAGMA foreign_keys = OFF")
    new_sql = row[0].replace("CREATE TABLE users", "CREATE TABLE users_v3", 1) \
                    .replace("CHECK(role IN ('admin','hr','viewer'))", "CHECK(role IN ('admin','hr','ga','viewer'))")
    conn.execute(new_sql)
    conn.execute("INSERT INTO users_v3 SELECT * FROM users")
    conn.execute("DROP TABLE users")
    conn.execute("ALTER TABLE users_v3 RENAME TO users")
    conn.execute("PRAGMA foreign_keys = ON")


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


def get_behaviors(group: str) -> dict[str, str]:
    return {r["name"]: r["behavior"] or "" for r in query("SELECT name, behavior FROM codes WHERE group_code=?", (group,))}


def audit(module: str, action: str, target: str = "", detail: str = ""):
    """감사로그 기록. 개인정보 원문(연봉·연락처 등)은 detail 에 넣지 않습니다."""
    uid, uname, ip = current_actor.get()
    execute(
        "INSERT INTO audit_logs(user_id, username, ip, module, action, target, detail) VALUES (?,?,?,?,?,?,?)",
        (uid, uname, ip, module, action, str(target or ""), str(detail or "")),
    )
