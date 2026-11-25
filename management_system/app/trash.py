"""휴지통: 화면에서 지운 자료를 바로 없애지 않고 보관했다가 되살리거나 완전히 지웁니다.

- 지울 때(remove): 그 자료와 함께 지워지는 딸린 자료(이력·체크·사진 기록 등)를 통째로 담아 두고(암호화) 원래 표에서 지움.
  다른 자료에서 가리키던 칸(담당자 등)이 비워지면 그 칸도 적어 둠.
- 되살리기(restore): 같은 번호로 원래 자리에 다시 넣음 → 이력·붙임 서류가 그대로 이어짐. 같은 자료가 새로 있거나
  딸려 있던 직원·차량이 지금 없으면 되살리지 않고 이유를 알려 줌.
- 완전삭제(purge): 관리자만. 60일이 지나면 자동으로(auto_purge). 단 법정 보존 서류가 섞인 자료(KEEP)는 자동으로 지우지 않음.
- 문서 보관함(NAS)의 파일·폴더는 어느 경우에도 건드리지 않음. 서버 안에 작게 보관한 자산·점검 사진만 완전삭제 때 함께 지움.
- 볼 수 있는 권한은 그 메뉴를 다루는 권한과 같음(META 의 roles).
"""
import base64
import json
import logging
import sqlite3
import threading
from collections import deque
from datetime import date, datetime, timedelta

from app.common import to_date, today
from app.db import audit, batch, current_actor, execute, query, query_one
from app.security.crypto import dec, enc

log = logging.getLogger("bms.trash")
DAYS = 60            # 자동 완전삭제까지
WARN_DAYS = 7        # 이만큼 남으면 대시보드에 알림
MIN_SEEN = 45        # 자동 완전삭제 전에 실제로 지켜본 날 수 (서버가 꺼져 있던 날은 세지 않음)
MOVED = "휴지통으로 옮겼습니다"
ADMIN, HR, GA = {"admin"}, {"admin", "hr"}, {"admin", "hr", "ga"}
SKIP_TABLES = {"ga_vehicle_access", "sessions", "mfa_devices", "mfa_state"}      # 잠깐 쓰는 접속 기록은 담지 않음


class TrashError(Exception):
    pass


# ── 자료 종류: (이름, 메뉴, 권한, 자동삭제 제외, 제목, 고치러 갈 주소, 내용으로 보여 줄 칸) ──────────────
def _emp(i) -> str:
    r = query_one("SELECT name FROM employees WHERE id=?", (i,)) if i else None
    return r["name"] if r else "(직원)"


def _car(i) -> str:
    r = query_one("SELECT plate_no FROM ga_vehicles WHERE id=?", (i,)) if i else None
    return r["plate_no"] if r else "(차량)"


def _one(table, col, i) -> str:
    r = query_one(f"SELECT {col} x FROM {table} WHERE id=?", (i,)) if i else None
    return str(r["x"]) if r else ""


def _y(v) -> str:
    return str(v or "")[:4] or str(today().year)


def _term(r, a="start_date", b="end_date") -> str:
    return f"{r.get(a) or ''}~{r.get(b) or ''}"


META = {
    # 인사
    "employees": ("임직원", "임직원 현황", HR, True, lambda r: f"{r['name']} ({r['emp_no']})", lambda r: f"/hr/employees/{r['id']}/edit",
                  [("emp_no", "사번"), ("name", "성명"), ("department", "부서"), ("rank", "직급"), ("hire_date", "입사일"), ("resign_date", "퇴사일")]),
    "hr_documents": ("근로계약서·증명서", "근로계약서 / 증명서 발급", HR, True, lambda r: f"{_emp(r['employee_id'])} · {r['title']}" + (f" {r['doc_no']}" if r.get("doc_no") else ""),
                     lambda r: f"/hr/documents/{r['id']}", [("category", "종류"), ("title", "제목"), ("doc_no", "번호"), ("start_date", "시작"), ("end_date", "종료"), ("status", "상태"), ("created_at", "작성")]),
    "appointments": ("인사발령", "인사발령", HR, True, lambda r: f"{_emp(r['employee_id'])} · {r['appt_date']} {r['appt_type']}", None,
                     [("appt_date", "발령일"), ("appt_type", "구분"), ("from_value", "전"), ("to_value", "후"), ("note", "메모"), ("status", "상태")]),
    "salaries": ("연봉", "인건비·연봉", HR, True, lambda r: f"{_emp(r['employee_id'])} · {r['year']}년 연봉", lambda r: f"/hr/salaries/person/{r['employee_id']}",
                 [("year", "연도"), ("effective_date", "적용일"), ("reason", "사유"), ("note", "메모")]),
    "salary_steps": ("연중 연봉 변경", "인건비·연봉", HR, True, lambda r: f"{_emp(r['employee_id'])} · {r['effective_date']} 연봉 변경", lambda r: f"/hr/salaries/person/{r['employee_id']}",
                     [("year", "연도"), ("effective_date", "적용일"), ("reason", "사유")]),
    "rewards": ("상벌", "상벌현황", HR, True, lambda r: f"{_emp(r['employee_id'])} · {r['kind']} {r['category']} {r['action_date']}", lambda r: f"/hr/rewards?row={r['id']}",
                [("kind", "구분"), ("category", "종류"), ("action_date", "일자"), ("reason", "사유"), ("period_start", "징계 시작"), ("period_end", "징계 종료"), ("expunge_date", "말소예정"), ("issuer", "시행권자"), ("note", "메모")]),
    "work_specials": ("근로특이사항", "근로특이사항", HR, True, lambda r: f"{_emp(r['employee_id'])} · {r['category']} {_term(r)}", lambda r: f"/hr/specials?row={r['id']}",
                      [("category", "구분"), ("start_date", "시작"), ("end_date", "종료"), ("actual_end", "실제 종료"), ("reduced_hours", "단축 후 시간"), ("replacement", "대체인력"), ("subsidy", "정부지원금"), ("note", "메모")]),
    "health_checks": ("건강검진", "건강검진", HR, True, lambda r: f"{_emp(r['employee_id'])} · {r['check_date']} {r['kind']}", lambda r: "/hr/health",
                      [("check_date", "검진일"), ("kind", "종류"), ("institution", "기관"), ("note", "메모")]),
    "training_records": ("교육 이수", "교육 이수", GA, True, lambda r: f"{_emp(r['employee_id'])} · {_one('training_courses', 'name', r['course_id'])} {r['edu_date']}",
                         lambda r: f"/mgmt/training?tab=records&year={_y(r['edu_date'])}", [("edu_date", "교육일"), ("hours", "시간"), ("method", "방법"), ("provider", "기관"), ("note", "메모")]),
    "dual_enrollments": ("프로젝트 이중가입", "프로젝트 이중가입", HR, False, lambda r: f"{_emp(r['employee_id'])} · {r['workplace']}", lambda r: f"/hr/dual?open={r['id']}&row={r['id']}",
                         [("workplace", "상대 사업장"), ("project", "프로젝트"), ("start_date", "시작"), ("end_date", "종료 예정"), ("actual_end", "실제 종료"), ("reason", "사유"), ("note", "메모")]),
    "employee_insurances": ("상해공제", "임직원 현황", HR, False, lambda r: f"{_emp(r['employee_id'])} · {r['product']}", lambda r: f"/hr/employees/{r['employee_id']}?tab=insurance",
                            [("product", "공제명"), ("insurer", "보험사"), ("start_date", "시작"), ("end_date", "만기"), ("policy_no", "증권번호"), ("note", "메모")]),
    "doc_templates": ("문서 템플릿", "문서 템플릿", HR, False, lambda r: r["name"], lambda r: f"/hr/templates/{r['id']}", [("category", "종류"), ("name", "이름"), ("updated_at", "고친 때")]),
    "offboardings": ("퇴사 절차", "퇴사 절차 안내", HR, False, lambda r: f"{_emp(r['employee_id'])} 퇴사 절차", lambda r: f"/hr/offboard/{r['employee_id']}", [("last_day", "마지막 근무일"), ("handover_due", "인계 기한"), ("successor", "인수자"), ("note", "메모")]),
    "onboardings": ("입사 절차", "입사 절차 안내", HR, False, lambda r: f"{_emp(r['employee_id'])} 입사 절차", lambda r: f"/hr/onboard/{r['employee_id']}", [("last_day", "입사일"), ("handover_due", "기한"), ("successor", "담당"), ("note", "메모")]),
    "incentive_payments": ("인센티브 지급", "인센티브", HR, False, lambda r: f"{_emp(r['employee_id']) if r.get('employee_id') else r['department']} · {r['pay_date']} 인센티브", lambda r: f"/mgmt/incentive?year={r['year']}",
                           [("pay_date", "지급일"), ("department", "부서"), ("reason", "사유"), ("note", "메모")]),
    # 교육·법정의무
    "compliance_tasks": ("법정의무 일정", "법정의무 일정", GA, False, lambda r: r["title"], lambda r: "/mgmt/compliance?show=all#tasks", [("title", "일정"), ("category", "구분"), ("cycle", "주기"), ("basis", "근거"), ("owner", "담당"), ("note", "메모")]),
    # 총무
    "ga_contracts": ("계약", "계약·만기 관리", GA, False, lambda r: f"{r['title']} ({r['counterparty'] or ''})", lambda r: f"/ga/contracts/{r['id']}",
                     [("category", "구분"), ("title", "계약명"), ("counterparty", "상대방"), ("start_date", "시작"), ("end_date", "종료"), ("status", "상태"), ("note", "메모")]),
    "ga_company_items": ("면허·보험·인증", "계약·만기 관리", GA, False, lambda r: f"{r['category']} {r['name']}", lambda r: f"/ga/contracts/item/{r['id']}",
                         [("category", "구분"), ("name", "이름"), ("number", "번호"), ("agency", "기관"), ("expire_date", "만기"), ("owner", "담당"), ("note", "메모")]),
    "ga_docs": ("문서 수발", "문서수발·관인", GA, False, lambda r: f"{r['doc_no']} {r['title']}", lambda r: f"/ga/docs?year={_y(r['doc_date'])}&row={r['id']}",
                [("direction", "구분"), ("doc_no", "번호"), ("doc_date", "일자"), ("counterpart", "상대"), ("title", "제목"), ("status", "상태"), ("note", "메모")]),
    "ga_seal_logs": ("관인 사용", "문서수발·관인", GA, False, lambda r: f"{r['use_date']} {r['seal_type']} {r['purpose']}", lambda r: f"/ga/docs?tab=seal&year={_y(r['use_date'])}&row={r['id']}",
                     [("use_date", "사용일"), ("seal_type", "인장"), ("purpose", "용도"), ("submit_to", "제출처"), ("status", "상태"), ("approver", "승인")]),
    "ga_events": ("경조사", "경조사", GA, False, lambda r: f"{_emp(r['employee_id'])} · {r['category']} {r['relation']} {r['event_date']}", lambda r: f"/ga/events?year={_y(r['event_date'])}&row={r['id']}",
                  [("event_date", "일자"), ("category", "구분"), ("relation", "대상"), ("status", "상태"), ("paid_date", "지급일")]),
    "ga_event_rules": ("경조 규정", "경조사", GA, False, lambda r: f"{r['category']} {r['relation']}", lambda r: "/ga/events", [("category", "구분"), ("relation", "대상"), ("leave_days", "휴가")]),
    "ga_assets": ("자산·비품", "자산·비품", GA, False, lambda r: f"{r['asset_no']} {r['name']}", lambda r: f"/ga/assets/{r['id']}",
                  [("asset_no", "자산번호"), ("category", "분류"), ("name", "이름"), ("model", "모델"), ("status", "상태"), ("location", "위치"), ("memo", "메모")]),
    "ga_asset_history": ("자산 이력", "자산·비품", GA, False, lambda r: f"{_one('ga_assets', 'asset_no', r['asset_id'])} · {r['event_date']} {r['event_type']}", lambda r: f"/ga/assets/{r['asset_id']}",
                         [("event_date", "일자"), ("event_type", "구분"), ("note", "메모")]),
    "ga_vehicles": ("법인차량", "법인차량", GA, False, lambda r: f"{r['plate_no']} {r['model']}", lambda r: f"/ga/vehicles/{r['id']}", [("plate_no", "차량번호"), ("model", "차종"), ("ownership", "소유"), ("status", "상태"), ("memo", "메모")]),
    "ga_vehicle_logs": ("운행기록", "법인차량", GA, False, lambda r: f"{_car(r['vehicle_id'])} · {r['drive_date']} 운행", lambda r: f"/ga/vehicles/{r['vehicle_id']}", [("drive_date", "일자"), ("purpose", "목적"), ("km_start", "출발 km"), ("km_end", "도착 km"), ("note", "메모")]),
    "ga_vehicle_maint": ("차량 정비", "법인차량", GA, False, lambda r: f"{_car(r['vehicle_id'])} · {r['maint_date']} {r.get('category') or ''}", lambda r: f"/ga/vehicles/{r['vehicle_id']}", [("maint_date", "일자"), ("category", "구분"), ("detail", "내용"), ("km", "km"), ("vendor", "업체")]),
    "ga_vehicle_fines": ("과태료", "법인차량", GA, False, lambda r: f"{_car(r['vehicle_id'])} · {r['violation_at']} {r['kind']}", lambda r: f"/ga/vehicles?tab=fines&year={_y(r['violation_at'])}&row={r['id']}",
                         [("violation_at", "위반 일시"), ("kind", "내용"), ("place", "장소"), ("due_date", "납부 기한"), ("status", "진행")]),
    "ga_vehicle_damages": ("사고·파손", "법인차량", GA, False, lambda r: f"{_car(r['vehicle_id'])} · {r['occur_date']} {r['kind']} {r['part'] or ''}", lambda r: f"/ga/vehicles?tab=damages&year={_y(r['occur_date'])}&row={r['id']}",
                           [("occur_date", "일자"), ("kind", "구분"), ("part", "부위"), ("detail", "내용"), ("responsibility", "책임"), ("status", "진행")]),
    "ga_vehicle_inspections": ("차량 사진 점검", "법인차량", GA, False, lambda r: f"{_car(r['vehicle_id'])} · {r['insp_date']} {r['kind']} 점검", lambda r: f"/ga/vehicles/{r['vehicle_id']}/inspections/{r['id']}", [("insp_date", "일자"), ("kind", "구분"), ("status", "상태")]),
    # 시스템
    "codes": ("구분값", "구분값 설정", ADMIN, False, lambda r: f"{r['name']}", lambda r: f"/settings/codes?group={r['group_code']}", [("group_code", "구분"), ("name", "이름")]),
}
CHILD = {"ga_company_history": "갱신 이력", "hr_doc_mails": "메일 기록", "hr_sign_requests": "서명 요청", "ga_vehicle_photos": "점검 사진", "ga_vehicle_assign": "사용자 이력", "employee_rrn": "주민번호", "employee_payroll": "급여 정보",
         "employee_qualifications": "자격", "employee_checks": "체크", "leave_records": "휴가 기록", "leave_adjust": "연차 조정", "leave_promotions": "연차 촉진", "leave_plans": "연차 계획", "leave_status": "연차 현황",
         "compliance_done": "처리 기록", "training_targets": "교육 대상", "review_items": "평가", "offboard_checks": "체크", "offboard_mails": "안내 메일", "onboard_checks": "체크", "onboard_mails": "안내 메일"}


def label(table: str) -> str:
    return META[table][0] if table in META else CHILD.get(table, "딸린 기록")


# ── 표 구조 읽기 ────────────────────────────────────────────────────────
_cache: dict = {}


def _schema(conn) -> tuple[dict, dict]:
    """(표 → 외래키 목록, 표 → 칸 목록). 외래키: (칸, 부모 표, 부모 칸, 지울 때 동작)."""
    from app.db import DB_PATH
    ver = (DB_PATH, conn.execute("PRAGMA schema_version").fetchone()[0])
    if _cache.get("ver") == ver:
        return _cache["val"]
    fks, cols = {}, {}
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
        info = conn.execute(f"PRAGMA table_info({name})").fetchall()
        cols[name] = {c[1]: {"notnull": bool(c[3]) or bool(c[5]), "pk": c[5]} for c in info}
        fks[name] = [(f[3], f[2], f[4] or "id", (f[6] or "").upper()) for f in conn.execute(f"PRAGMA foreign_key_list({name})").fetchall()]
    _cache.update(ver=ver, val=(fks, cols))
    return fks, cols


def _pack(v):
    return {"b64": base64.b64encode(v).decode()} if isinstance(v, (bytes, memoryview)) else v


def _unpack(v):
    return base64.b64decode(v["b64"]) if isinstance(v, dict) and "b64" in v else v


def _rows(conn, table, col, val) -> list[dict]:
    return [dict(r) for r in conn.execute(f"SELECT rowid AS _rowid, * FROM {table} WHERE {col}=?", (val,)).fetchall()]


# ── 지우기 ──────────────────────────────────────────────────────────────
def remove(table: str, rid: int, pk: str = "id", extra: tuple = (), audit_note: tuple | None = None) -> int:
    """자료 하나를 휴지통으로. extra: 외래키로 이어지지 않았지만 함께 지울 (표, 칸, 값). 없는 자료면 0. 휴지통 번호를 돌려줌."""
    if table not in META:
        raise ValueError(table)
    with batch() as conn:
        root = _rows(conn, table, pk, rid)
        if not root:
            return 0
        title = str(META[table][4](query_one(f"SELECT * FROM {table} WHERE {pk}=?", (rid,))))[:200]
        fks, _cols = _schema(conn)
        kids: dict = {}
        for t, lst in fks.items():
            for col, parent, pcol, act in lst:
                kids.setdefault(parent, []).append((t, col, pcol, act))
        items, seen, refs = [], set(), []
        todo = deque((table, r) for r in root)
        for t, col, val in extra:
            todo.extend((t, r) for r in _rows(conn, t, col, val))
        while todo:
            t, r = todo.popleft()
            key = (t, r["_rowid"])
            if key in seen or t in SKIP_TABLES:
                continue
            seen.add(key)
            items.append((t, r))
            for ct, col, pcol, act in kids.get(t, []):
                if r.get(pcol) is None or ct in SKIP_TABLES:
                    continue
                if act == "CASCADE":
                    todo.extend((ct, x) for x in _rows(conn, ct, col, r[pcol]))
                elif act == "SET NULL":
                    refs += [(ct, x["_rowid"], col, r[pcol]) for x in conn.execute(f"SELECT rowid AS _rowid FROM {ct} WHERE {col}=?", (r[pcol],)).fetchall()]
        refs = [x for x in refs if (x[0], x[1]) not in seen]
        data = {"rows": [[t, {k: _pack(v) for k, v in r.items() if k != "_rowid"}] for t, r in items], "refs": refs, "extra": [list(x[:2]) for x in extra]}
        counts: dict = {}
        for t, _r in items[1:]:
            counts[label(t)] = counts.get(label(t), 0) + 1
        uid, who, _ip = current_actor.get()
        tid = execute("INSERT INTO trash_items(table_name, root_id, title, kids, data, deleted_by, deleted_at) VALUES (?,?,?,?,?,?,?)",
                      (table, int(rid), title, json.dumps(counts, ensure_ascii=False), enc(json.dumps(data, ensure_ascii=False)), _who(uid, who), _now()))
        for t, col, val in extra:
            conn.execute(f"DELETE FROM {t} WHERE {col}=?", (val,))
        conn.execute(f"DELETE FROM {table} WHERE {pk}=?", (rid,))
        audit(*(audit_note or ("trash", f"{META[table][0]} 삭제", f"#{rid}", "휴지통")))
    return tid


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _who(uid, username) -> str:
    r = query_one("SELECT display_name FROM users WHERE id=?", (uid,)) if uid else None
    return (r["display_name"] if r else username) or ""


# ── 목록 ────────────────────────────────────────────────────────────────
def _expire(r) -> date | None:
    d = to_date(r["deleted_at"])
    return None if META.get(r["table_name"], (0, 0, 0, True))[3] or not d else d + timedelta(days=DAYS)


def allowed(table: str, role: str | None) -> bool:
    return table in META and role in META[table][2]


def items(role: str, table: str = "", q: str = "") -> list[dict]:
    out = []
    for r in query("SELECT id, table_name, root_id, title, kids, deleted_by, deleted_at FROM trash_items ORDER BY id DESC"):
        t = r["table_name"]
        if not allowed(t, role) or (table and t != table) or (q and q.lower() not in f"{r['title']} {r['deleted_by']} {META[t][0]}".lower()):
            continue
        x = _expire(r)
        kids = json.loads(r["kids"] or "{}")
        out.append({**r, "label": META[t][0], "menu": META[t][1], "keep": META[t][3], "editable": bool(META[t][5]), "kids": kids,
                    "left": max(0, (x - today()).days) if x else None})
    return out


def kinds(role: str) -> list[tuple[str, str, int]]:
    """휴지통에 있는 종류: (표, 이름, 건수)."""
    n = {r["table_name"]: r["c"] for r in query("SELECT table_name, COUNT(*) c FROM trash_items GROUP BY table_name")}
    return [(t, m[0], n[t]) for t, m in META.items() if t in n and role in m[2]]


def soon(role: str) -> int:
    """곧(7일 안) 자동으로 완전삭제될 건수."""
    cut = (today() - timedelta(days=DAYS - WARN_DAYS)).isoformat()
    return sum(r["c"] for r in query("SELECT table_name, COUNT(*) c FROM trash_items WHERE substr(deleted_at,1,10)<=? GROUP BY table_name", (cut,))
               if allowed(r["table_name"], role) and not META[r["table_name"]][3])


def _load(tid: int, role: str | None) -> tuple[dict, dict]:
    r = query_one("SELECT * FROM trash_items WHERE id=?", (tid,))
    if not r or not allowed(r["table_name"], role):
        raise TrashError("휴지통에서 찾을 수 없습니다. 이미 되살렸거나 완전히 지운 자료입니다")
    try:
        data = json.loads(r["data"])
        assert isinstance(data["rows"], list) and data["rows"]
    except Exception as ex:                                  # noqa: BLE001
        raise TrashError("휴지통 자료를 읽을 수 없습니다. 관리자에게 알려 주세요") from ex
    return r, data


def detail(tid: int, role: str) -> list[tuple[str, str]]:
    """내용 보기: 정해 둔 칸만 (연봉·금액·주민번호 등은 담지 않음)."""
    r, data = _load(tid, role)
    row = data["rows"][0][1]
    out = []
    for col, name in META[r["table_name"]][6]:
        v = dec(row.get(col))
        if v not in (None, ""):
            out.append((name, str(v)[:500]))
    return out


# ── 되살리기 ────────────────────────────────────────────────────────────
def restore(tid: int, role: str) -> tuple[str, str]:
    """(제목과 안내, 고치러 갈 주소). 되살릴 수 없으면 TrashError."""
    with batch() as conn:
        r, data = _load(tid, role)
        table = r["table_name"]
        fks, cols = _schema(conn)
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        conn.execute("PRAGMA defer_foreign_keys=ON")             # 넣는 순서와 상관없이, 저장할 때 한 번에 확인
        rows = [(t, {k: _unpack(v) for k, v in x.items()}) for t, x in data["rows"] if t in cols]
        if not rows or rows[0][0] != table:
            raise TrashError("휴지통 자료를 읽을 수 없습니다. 관리자에게 알려 주세요")
        _check(conn, table, rows[0][1])
        rows, dropped = _link(conn, rows, fks, cols)
        added = 0
        for i, (t, x) in enumerate(rows):
            use = [k for k in x if k in cols[t]]
            conn.execute("SAVEPOINT tr")
            try:
                conn.execute(f"INSERT INTO {t}({', '.join(use)}) VALUES ({', '.join('?' * len(use))})", [x[k] for k in use])
                added += 1
            except sqlite3.IntegrityError as ex:
                conn.execute("ROLLBACK TO tr")
                if i == 0:
                    raise TrashError(f"같은 {META[table][0]} 자료가 이미 있어 되살릴 수 없습니다. 지금 있는 자료를 먼저 확인해 주세요") from ex
                dropped += 1
            conn.execute("RELEASE tr")
        for t, rowid, col, val in data.get("refs") or []:    # 비워졌던 칸(담당자 등): 아직 비어 있을 때만 되돌림
            if t in cols and col in cols[t]:
                conn.execute(f"UPDATE {t} SET {col}=? WHERE rowid=? AND {col} IS NULL", (val, rowid))
        root = rows[0][1]
        note = _after(conn, table, root)
        conn.execute("DELETE FROM trash_items WHERE id=?", (tid,))
        audit("trash", "휴지통 복구", f"{META[table][0]} #{r['root_id']}", f"딸린 기록 {added - 1}건" if added > 1 else "")
    _folder(table, r["root_id"])
    url = META[table][5]
    got = query_one(f"SELECT * FROM {table} WHERE {'employee_id' if 'id' not in cols[table] else 'id'}=?", (r["root_id"],))
    return (f"{META[table][0]} '{r['title']}'을(를) 되살렸습니다" + (f" (겹치거나 이어질 곳이 없는 기록 {dropped}건은 뺐습니다)" if dropped else "") + note,
            url(got) if url and got else "")


def _link(conn, rows, fks, cols):
    """이어질 부모가 지금 없는 줄: 비워도 되는 칸이면 비우고, 꼭 있어야 하면 그 줄을 뺌. 맨 위 자료면 되살리지 않음."""
    def exists(t, col, val):
        return conn.execute(f"SELECT 1 FROM {t} WHERE {col}=?", (val,)).fetchone() is not None
    dropped = 0
    while True:
        have: dict = {}
        for t, x in rows:
            have.setdefault(t, []).append(x)
        idx = {}
        keep, again = [], False
        for i, (t, x) in enumerate(rows):
            ok = True
            for col, parent, pcol, _act in fks.get(t, []):
                v = x.get(col)
                if v is None:
                    continue
                if (parent, pcol) not in idx:
                    idx[(parent, pcol)] = {p.get(pcol) for p in have.get(parent, [])}
                if v in idx[(parent, pcol)] or exists(parent, pcol, v):
                    continue
                if not cols[t][col]["notnull"]:
                    x[col] = None
                elif i == 0:
                    raise TrashError({"employees": "그 직원이 지금 없습니다. 휴지통에서 직원을 먼저 되살려 주세요",
                                      "ga_vehicles": "그 차량이 지금 없습니다. 휴지통에서 차량을 먼저 되살려 주세요",
                                      "ga_assets": "그 자산이 지금 없습니다. 휴지통에서 자산을 먼저 되살려 주세요"}.get(parent, "이어져 있던 자료가 지금 없어 되살릴 수 없습니다"))
                else:
                    ok = False
            if ok:
                keep.append((t, x))
            else:
                dropped, again = dropped + 1, True
        rows = keep
        if not again:
            return rows, dropped


def _check(conn, table, root):
    if table == "hr_documents" and root.get("doc_no") and conn.execute("SELECT 1 FROM hr_documents WHERE doc_no=?", (root["doc_no"],)).fetchone():
        raise TrashError(f"같은 번호({root['doc_no']})의 증명서가 이미 있어 되살릴 수 없습니다")


def _after(conn, table, root) -> str:
    """되살린 뒤 맞춰야 할 것."""
    if table == "hr_documents" and root.get("doc_group") == "certificate":     # 되돌려 둔 번호를 다음 발급이 또 쓰지 않게
        import re
        m = re.match(r"제(\d{4})-(\d+)호", str(root.get("doc_no") or ""))
        if m:
            conn.execute("INSERT INTO settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value WHERE CAST(value AS INTEGER) < CAST(excluded.value AS INTEGER)",
                         (f"cert_seq_{m.group(1)}", str(int(m.group(2)))))
    if table == "appointments" and root.get("status") == "예정" and (to_date(root.get("appt_date")) or today()) <= today():
        conn.execute("UPDATE appointments SET status='이력' WHERE id=?", (root["id"],))
        return ". 발령일이 이미 지나 '이력'으로 되살렸습니다 (인사·고용 정보는 그대로입니다)"
    if table == "ga_asset_history":
        from app.modules.ga import repo
        repo.rebuild_asset_state(root["asset_id"])
    return ""


FOLDER_MENU = {"employees": "hr.employees", "rewards": "hr.rewards", "ga_vehicles": "ga.vehicles", "ga_vehicle_fines": "ga.fines", "ga_vehicle_damages": "ga.damages",
               "ga_contracts": "ga.contracts", "ga_docs": "ga.docs", "ga_company_items": "ga.company"}


def _folder(table, rid):
    """서류 폴더 이름을 다시 맞춤(없을 때만 만듦). 실패해도 되살리기는 그대로."""
    if table in FOLDER_MENU:
        from app import archive
        archive.ensure_quiet(FOLDER_MENU[table], rid)


# ── 완전삭제 ────────────────────────────────────────────────────────────
def purge(tid: int, role: str, auto: bool = False) -> str:
    if role != "admin":
        raise TrashError("완전삭제는 시스템관리자만 할 수 있습니다")
    with batch():
        r, data = _load(tid, role)
        execute("DELETE FROM trash_items WHERE id=?", (tid,))
        audit("trash", "휴지통 자동 완전삭제" if auto else "휴지통 완전삭제", f"{META[r['table_name']][0]} #{r['root_id']}", f"지운 때 {r['deleted_at'][:10]}")
    _purge_files(data)
    return r["title"]


def _purge_files(data: dict):
    """서버 안에 작게 보관한 사진만 지움. 문서 보관함(NAS)의 파일은 건드리지 않음."""
    from app.modules.ga import photos
    for t, x in data["rows"]:
        try:
            if t == "ga_assets":
                photos.delete(x["id"])
            elif t == "ga_vehicle_photos":
                photos.delete_vehicle(x["inspection_id"], x["id"])
        except OSError:
            log.exception("휴지통 사진 정리 실패: %s", t)


def auto_purge() -> int:
    """지운 지 60일이 지난 자료를 완전히 지움 (법정 보존 자료는 제외).
    서버 시계가 잘못돼 앞으로 건너뛰어도 한꺼번에 지워지지 않게, 실제로 MIN_SEEN 일 이상 지켜본 자료만 지움."""
    day = today().isoformat()
    execute("UPDATE trash_items SET seen_days=seen_days+1, seen_on=? WHERE seen_on IS NULL OR seen_on<>?", (day, day))
    n = 0
    for r in query("SELECT id, table_name, deleted_at, seen_days FROM trash_items"):
        x = _expire(r)
        if x and x <= today() and r["seen_days"] >= MIN_SEEN:
            try:
                purge(r["id"], "admin", auto=True)
                n += 1
            except TrashError:
                log.exception("휴지통 자동 완전삭제 실패: #%s", r["id"])
    return n


_stop = threading.Event()
_thread = None


def _loop():
    while not _stop.wait(3600):
        try:
            auto_purge()
        except Exception:                                    # noqa: BLE001  (일시 오류로 멈추지 않게)
            log.exception("휴지통 자동 정리 오류")


def start_worker():
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="trash", daemon=True)
    _thread.start()


def stop_worker():
    _stop.set()
