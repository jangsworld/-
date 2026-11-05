"""인사관리 데이터 접근 계층."""
import json
import re

from app.common import today, to_date
from app.db import query, query_one, execute, tx, audit
from app.security.crypto import enc
from app.modules.hr import rrn as rrn_util
from app.modules.hr.constants import LEAVE_CATEGORIES, REDUCE_CATEGORIES

EMPLOYEE_FIELDS = [
    "emp_no", "name", "birth_date", "gender", "phone", "email", "address",
    "emergency_contact", "emergency_relation", "department", "rank", "title", "job_duty",
    "employment_type", "work_location", "hire_date", "probation_end",
    "resign_date", "resign_reason", "bank_name", "bank_account",
    "education", "edu_level", "edu_school", "edu_major", "edu_grad_year",
    "certificates", "memo", "folder_name",
]

# AES-256-GCM 으로 암호화 저장되는 개인정보 항목
ENCRYPTED_EMPLOYEE_FIELDS = {
    "birth_date", "phone", "email", "address", "emergency_contact", "emergency_relation",
    "bank_account", "memo", "resign_reason",
}
ENCRYPTED_DOC_FIELDS = {"content", "params", "annual_salary"}

# 변경 시 인사발령 이력이 자동 기록되는 항목
TRACKED_FIELDS = {
    "department": "전보",
    "rank": "승진",
    "title": "직책변경",
    "employment_type": "고용형태변경",
}


# ════════════════════════════════════════════════════════════════════════
# 임직원
# ════════════════════════════════════════════════════════════════════════
def _special_status(s: dict, ref=None) -> str:
    ref = ref or today()
    start = to_date(s.get("start_date"))
    end = to_date(s.get("actual_end")) or to_date(s.get("end_date"))
    if start and start > ref:
        return "예정"
    if end and end < ref:
        return "종료"
    return "진행중"


def active_specials(ref=None) -> list[dict]:
    rows = query(
        "SELECT ws.*, e.name, e.department, e.emp_no FROM work_specials ws "
        "JOIN employees e ON e.id = ws.employee_id"
    )
    return [r for r in rows if _special_status(r, ref) == "진행중"]


def _status_map() -> dict[int, str]:
    """임직원별 현재 상태(휴직/단축근무) 계산."""
    out = {}
    for s in active_specials():
        if s["category"] in LEAVE_CATEGORIES:
            out[s["employee_id"]] = "휴직"
        elif s["category"] in REDUCE_CATEGORIES and out.get(s["employee_id"]) != "휴직":
            out[s["employee_id"]] = "단축근무"
    return out


def employee_status(emp: dict, status_map=None) -> str:
    rd = to_date(emp.get("resign_date"))
    if rd and rd <= today():
        return "퇴사"
    if to_date(emp.get("hire_date")) and to_date(emp["hire_date"]) > today():
        return "입사예정"
    sm = status_map if status_map is not None else _status_map()
    return sm.get(emp["id"], "재직")


def list_employees(q="", department="", employment_type="", status="") -> list[dict]:
    sql = "SELECT * FROM employees WHERE 1=1"
    params = []
    if department:
        sql += " AND department = ?"
        params.append(department)
    if employment_type:
        sql += " AND employment_type = ?"
        params.append(employment_type)
    sql += " ORDER BY CASE WHEN resign_date IS NULL OR resign_date='' THEN 0 ELSE 1 END, hire_date, emp_no"
    rows = query(sql, params)
    if q:  # 연락처·이메일은 암호화되어 있으므로 복호화 후 검색
        q = q.strip().lower()
        rows = [r for r in rows if any(q in str(r.get(f) or "").lower()
                                       for f in ("name", "emp_no", "phone", "email"))]

    sm = _status_map()
    cur_sal = current_salary_map()
    contract_ok = valid_contract_ids()
    for r in rows:
        r["status"] = employee_status(r, sm)
        r["annual_salary"] = cur_sal.get(r["id"])
        r["has_contract"] = r["id"] in contract_ok
    if status:
        if status == "재직(전체)":
            rows = [r for r in rows if r["status"] in ("재직", "휴직", "단축근무")]
        else:
            rows = [r for r in rows if r["status"] == status]
    return rows


def get_employee(emp_id: int) -> dict | None:
    e = query_one("SELECT * FROM employees WHERE id=?", (emp_id,))
    if e:
        e["status"] = employee_status(e)
        e["annual_salary"] = current_salary_map().get(emp_id)
    return e


def next_emp_no(hire_date: str | None = None) -> str:
    year = (to_date(hire_date) or today()).year
    row = query_one(
        "SELECT emp_no FROM employees WHERE emp_no LIKE ? ORDER BY emp_no DESC LIMIT 1",
        (f"{year}%",),
    )
    seq = 1
    if row:
        try:
            seq = int(row["emp_no"][4:]) + 1
        except ValueError:
            seq = query_one("SELECT COUNT(*) c FROM employees")["c"] + 1
    return f"{year}{seq:03d}"


def education_summary(data: dict) -> str | None:
    year = data.get("edu_grad_year")
    parts = [data.get("edu_level"), data.get("edu_school"), data.get("edu_major"), f"{year}년 졸업" if year else None]
    return " · ".join(p for p in parts if p) or None


def _clean(data: dict) -> dict:
    data = dict(data)
    if any(k in data for k in ("edu_level", "edu_school", "edu_major", "edu_grad_year")):
        data["education"] = education_summary(data)
    if "bank_account" in data:  # 계좌번호는 숫자만 저장 (은행별 자리 구분이 다르고 대량이체 파일은 숫자만 사용)
        data["bank_account"] = re.sub(r"\D", "", str(data["bank_account"] or ""))
    out = {}
    for f in EMPLOYEE_FIELDS:
        if f in data:
            v = data[f]
            out[f] = v.strip() if isinstance(v, str) else v
            if out[f] == "":
                out[f] = None
            if f in ENCRYPTED_EMPLOYEE_FIELDS:
                out[f] = enc(out[f])
    return out


def create_employee(data: dict) -> int:
    d = _clean(data)
    if not d.get("emp_no"):
        d["emp_no"] = next_emp_no(d.get("hire_date"))
    cols = ", ".join(d.keys())
    ph = ", ".join("?" * len(d))
    emp_id = execute(f"INSERT INTO employees ({cols}) VALUES ({ph})", list(d.values()))
    add_appointment(emp_id, d.get("hire_date") or today().isoformat(), "입사", None,
                    " / ".join(x for x in [d.get("department"), d.get("rank")] if x), "신규 입사")
    audit("hr", "임직원 등록", d.get("name", ""), d["emp_no"])
    return emp_id


def update_employee(emp_id: int, data: dict, appt_date: str | None = None):
    before = query_one("SELECT * FROM employees WHERE id=?", (emp_id,))
    if not before:
        return
    d = _clean(data)
    if not d.get("emp_no"):
        d.pop("emp_no", None)
    sets = ", ".join(f"{k}=?" for k in d) + ", updated_at=datetime('now','localtime')"
    execute(f"UPDATE employees SET {sets} WHERE id=?", list(d.values()) + [emp_id])

    when = appt_date or today().isoformat()
    for field, appt_type in TRACKED_FIELDS.items():
        if field in d and (before.get(field) or None) != d[field]:
            add_appointment(emp_id, when, appt_type, before.get(field), d[field], "정보 변경 자동기록")
    if d.get("resign_date") and not before.get("resign_date"):
        add_appointment(emp_id, d["resign_date"], "퇴사", None, None, d.get("resign_reason") or "")
    audit("hr", "임직원 수정", before["name"], json.dumps(list(d.keys()), ensure_ascii=False))


def employment_types_in_use() -> list[str]:
    return [r["employment_type"] for r in query("SELECT DISTINCT employment_type FROM employees")]


# 주민등록번호는 이 세 함수로만 다룹니다. 화면·문서·CSV 에는 생년월일을 사용합니다.
def set_rrn(emp_id: int, rrn: str):
    execute("INSERT INTO employee_rrn(employee_id, rrn) VALUES (?,?) ON CONFLICT(employee_id) DO UPDATE SET "
            "rrn=excluded.rrn, updated_at=datetime('now','localtime')", (emp_id, enc(rrn)))
    audit("hr", "주민등록번호 저장", f"emp#{emp_id}", "")


def get_rrn(emp_id: int) -> str | None:
    row = query_one("SELECT rrn FROM employee_rrn WHERE employee_id=?", (emp_id,))
    return row["rrn"] if row else None


def rrn_masked(emp_id: int) -> str:
    return rrn_util.mask(get_rrn(emp_id))


def list_technicians(kind: str = "") -> list[dict]:
    """보유자격 등록자 (퇴사자 제외)."""
    sql = ("SELECT q.*, e.name, e.emp_no, e.department, e.rank FROM employee_qualifications q "
           "JOIN employees e ON e.id=q.employee_id WHERE (e.resign_date IS NULL OR e.resign_date='' "
           "OR e.resign_date > date('now','localtime'))")
    params = []
    if kind:
        sql += " AND q.kind=?"
        params.append(kind)
    return query(sql + " ORDER BY q.kind, e.name", params)


def list_checks(emp_id: int) -> dict[tuple, dict]:
    rows = query("SELECT * FROM employee_checks WHERE employee_id=?", (emp_id,))
    return {(r["kind"], r["item"]): r for r in rows}


def set_check(emp_id: int, kind: str, item: str, done: bool, by: str):
    if done:
        execute("INSERT OR IGNORE INTO employee_checks(employee_id, kind, item, done_date, done_by) VALUES (?,?,?,?,?)",
                (emp_id, kind, item, today().isoformat(), by))
    else:
        execute("DELETE FROM employee_checks WHERE employee_id=? AND kind=? AND item=?", (emp_id, kind, item))


def get_qualifications(emp_id: int) -> dict[str, dict]:
    rows = query("SELECT * FROM employee_qualifications WHERE employee_id=?", (emp_id,))
    return {r["kind"]: r for r in rows}


def save_qualifications(emp_id: int, items: list[dict]):
    """입력값이 있는 자격은 저장하고, 모두 지운 자격은 삭제합니다."""
    for q in items:
        if any(q.get(k) for k in ("license_no", "grade", "main_cert", "expires_on")):
            execute(
                "INSERT INTO employee_qualifications(employee_id, kind, license_no, grade, main_cert, expires_on) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(employee_id, kind) DO UPDATE SET license_no=excluded.license_no, "
                "grade=excluded.grade, main_cert=excluded.main_cert, expires_on=excluded.expires_on",
                (emp_id, q["kind"], enc(q.get("license_no") or None), q.get("grade") or None, q.get("main_cert") or None,
                 q.get("expires_on") or None),
            )
        else:
            execute("DELETE FROM employee_qualifications WHERE employee_id=? AND kind=?", (emp_id, q["kind"]))


def match_employee_folder(folder_name: str) -> int | None:
    """폴더와 연결된 임직원 id. 같은 이름의 임직원이 한 명이면 폴더를 연결합니다."""
    row = query_one("SELECT id FROM employees WHERE folder_name=?", (folder_name,))
    if row:
        return row["id"]
    rows = query("SELECT id FROM employees WHERE name=? AND (folder_name IS NULL OR folder_name='')", (folder_name,))
    if len(rows) == 1:
        execute("UPDATE employees SET folder_name=? WHERE id=?", (folder_name, rows[0]["id"]))
        return rows[0]["id"]
    return None


# ════════════════════════════════════════════════════════════════════════
# 임직원 등록 초안
# ════════════════════════════════════════════════════════════════════════
def list_drafts(status: str = "") -> list[dict]:
    sql = "SELECT * FROM employee_drafts"
    params = []
    if status:
        sql += " WHERE status=?"
        params.append(status)
    return query(sql + " ORDER BY detected_at DESC, id DESC", params)


def get_draft(draft_id: int) -> dict | None:
    return query_one("SELECT * FROM employee_drafts WHERE id=?", (draft_id,))


def create_draft(folder_name: str, signature: str, data: dict) -> int:
    return execute("INSERT INTO employee_drafts(folder_name, signature, data) VALUES (?,?,?)",
                   (folder_name, signature, enc(data)))


def update_draft(draft_id: int, **kw):
    if "data" in kw:
        kw["data"] = enc(kw["data"])
    sets = ", ".join(f"{k}=?" for k in kw) + ", updated_at=datetime('now','localtime')"
    execute(f"UPDATE employee_drafts SET {sets} WHERE id=?", list(kw.values()) + [draft_id])


def delete_draft(draft_id: int):
    execute("DELETE FROM employee_drafts WHERE id=?", (draft_id,))


def delete_employee(emp_id: int):
    e = query_one("SELECT name, emp_no FROM employees WHERE id=?", (emp_id,))
    execute("DELETE FROM employees WHERE id=?", (emp_id,))
    if e:
        audit("hr", "임직원 삭제", e["name"], e["emp_no"])


# ════════════════════════════════════════════════════════════════════════
# 인사발령
# ════════════════════════════════════════════════════════════════════════
def add_appointment(emp_id, appt_date, appt_type, from_value, to_value, note=""):
    return execute(
        "INSERT INTO appointments(employee_id, appt_date, appt_type, from_value, to_value, note) "
        "VALUES (?,?,?,?,?,?)",
        (emp_id, appt_date, appt_type, from_value, to_value, note),
    )


def list_appointments(emp_id: int | None = None, limit: int | None = None) -> list[dict]:
    sql = ("SELECT a.*, e.name, e.emp_no, e.department FROM appointments a "
           "JOIN employees e ON e.id=a.employee_id")
    params = []
    if emp_id:
        sql += " WHERE a.employee_id=?"
        params.append(emp_id)
    sql += " ORDER BY a.appt_date DESC, a.id DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return query(sql, params)


def delete_appointment(appt_id: int):
    execute("DELETE FROM appointments WHERE id=?", (appt_id,))


# ════════════════════════════════════════════════════════════════════════
# 연봉
# ════════════════════════════════════════════════════════════════════════
def current_salary_map(year: int | None = None) -> dict[int, int]:
    """임직원별 기준연도 이하 최신 연봉."""
    year = year or today().year
    rows = query(
        "SELECT s.employee_id, s.annual_salary FROM salaries s "
        "JOIN (SELECT employee_id, MAX(year) y FROM salaries WHERE year<=? GROUP BY employee_id) m "
        "ON m.employee_id=s.employee_id AND m.y=s.year",
        (year,),
    )
    return {r["employee_id"]: r["annual_salary"] for r in rows}


def employee_salaries(emp_id: int) -> list[dict]:
    rows = query("SELECT * FROM salaries WHERE employee_id=? ORDER BY year", (emp_id,))
    prev = None
    for r in rows:
        r["prev_salary"] = prev
        if prev:
            r["raise_amount"] = r["annual_salary"] - prev
            r["raise_rate"] = (r["annual_salary"] - prev) / prev * 100
        else:
            r["raise_amount"] = r["raise_rate"] = None
        prev = r["annual_salary"]
    return list(reversed(rows))


def salary_years() -> list[int]:
    ys = [r["year"] for r in query("SELECT DISTINCT year FROM salaries ORDER BY year")]
    if today().year not in ys:
        ys.append(today().year)
    return sorted(ys)


def all_salaries() -> list[dict]:
    return query(
        "SELECT s.*, e.name, e.department, e.rank, e.emp_no, e.employment_type FROM salaries s "
        "JOIN employees e ON e.id=s.employee_id ORDER BY s.year, e.emp_no"
    )


def upsert_salary(emp_id, year, annual_salary, effective_date=None, reason="정기인상", note=""):
    execute(
        "INSERT INTO salaries(employee_id, year, effective_date, annual_salary, reason, note) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(employee_id, year) DO UPDATE SET "
        "effective_date=excluded.effective_date, annual_salary=excluded.annual_salary, "
        "reason=excluded.reason, note=excluded.note",
        (emp_id, int(year), effective_date or f"{int(year)}-01-01", enc(int(annual_salary)), reason, enc(note or None)),
    )


def delete_salary(sal_id: int):
    execute("DELETE FROM salaries WHERE id=?", (sal_id,))


# ════════════════════════════════════════════════════════════════════════
# 문서 템플릿 / 근로계약서 / 증명서
# ════════════════════════════════════════════════════════════════════════
def list_templates(doc_group: str | None = None, active_only=False) -> list[dict]:
    sql = "SELECT * FROM doc_templates WHERE 1=1"
    params = []
    if doc_group:
        sql += " AND doc_group=?"
        params.append(doc_group)
    if active_only:
        sql += " AND is_active=1"
    return query(sql + " ORDER BY doc_group, id", params)


def get_template(tid: int) -> dict | None:
    return query_one("SELECT * FROM doc_templates WHERE id=?", (tid,))


def save_template(tid, doc_group, category, name, content, is_active=1) -> int:
    if tid:
        execute(
            "UPDATE doc_templates SET doc_group=?, category=?, name=?, content=?, is_active=?, "
            "updated_at=datetime('now','localtime') WHERE id=?",
            (doc_group, category, name, content, int(is_active), tid),
        )
        return tid
    return execute(
        "INSERT INTO doc_templates(doc_group, category, name, content, is_active) VALUES (?,?,?,?,?)",
        (doc_group, category, name, content, int(is_active)),
    )


def delete_template(tid: int):
    execute("DELETE FROM doc_templates WHERE id=?", (tid,))


def list_documents(doc_group="contract", employee_id=None, status="", category="", q="") -> list[dict]:
    sql = ("SELECT d.id, d.doc_group, d.employee_id, d.template_id, d.category, d.title, d.doc_no, "
           "d.purpose, d.start_date, d.end_date, d.annual_salary, d.weekly_hours, d.status, "
           "d.signed_date, d.created_at, d.updated_at, e.name, e.emp_no, e.department, e.rank, "
           "e.employment_type, e.resign_date FROM hr_documents d JOIN employees e ON e.id=d.employee_id "
           "WHERE d.doc_group=?")
    params = [doc_group]
    if employee_id:
        sql += " AND d.employee_id=?"
        params.append(employee_id)
    if status:
        sql += " AND d.status=?"
        params.append(status)
    if category:
        sql += " AND d.category=?"
        params.append(category)
    if q:
        sql += " AND (e.name LIKE ? OR d.title LIKE ? OR e.emp_no LIKE ?)"
        params += [f"%{q}%"] * 3
    return query(sql + " ORDER BY d.created_at DESC, d.id DESC", params)


def get_document(doc_id: int) -> dict | None:
    return query_one(
        "SELECT d.*, e.name, e.emp_no, e.department, e.email, e.folder_name FROM hr_documents d "
        "JOIN employees e ON e.id=d.employee_id WHERE d.id=?",
        (doc_id,),
    )


def _enc_doc(kw: dict) -> dict:
    return {k: (enc(v) if k in ENCRYPTED_DOC_FIELDS else v) for k, v in kw.items()}


def create_document(**kw) -> int:
    kw = _enc_doc(kw)
    cols = ", ".join(kw.keys())
    ph = ", ".join("?" * len(kw))
    doc_id = execute(f"INSERT INTO hr_documents ({cols}) VALUES ({ph})", list(kw.values()))
    audit("hr", "문서 생성", kw.get("title", ""), str(doc_id))
    return doc_id


def update_document(doc_id: int, **kw):
    if not kw:
        return
    kw = _enc_doc(kw)
    sets = ", ".join(f"{k}=?" for k in kw) + ", updated_at=datetime('now','localtime')"
    execute(f"UPDATE hr_documents SET {sets} WHERE id=?", list(kw.values()) + [doc_id])


def delete_document(doc_id: int):
    execute("DELETE FROM hr_documents WHERE id=?", (doc_id,))


def valid_contract_ids(ref=None) -> set[int]:
    """기준일에 유효한(체결완료) 근로계약서를 보유한 임직원 id. 연봉계약서는 제외."""
    ref = (ref or today()).isoformat()
    rows = query(
        "SELECT DISTINCT employee_id FROM hr_documents WHERE doc_group='contract' "
        "AND category!='연봉계약' AND status='체결완료' AND (start_date IS NULL OR start_date<=?) "
        "AND (end_date IS NULL OR end_date='' OR end_date>=?)",
        (ref, ref),
    )
    return {r["employee_id"] for r in rows}


def next_cert_no() -> str:
    y = today().year
    row = query_one(
        "SELECT COUNT(*) c FROM hr_documents WHERE doc_group='certificate' AND doc_no LIKE ?",
        (f"제{y}-%",),
    )
    return f"제{y}-{row['c'] + 1:04d}호"


# ════════════════════════════════════════════════════════════════════════
# 상벌
# ════════════════════════════════════════════════════════════════════════
REWARD_FIELDS = ["employee_id", "kind", "category", "action_date", "reason", "amount",
                 "period_start", "period_end", "expunge_date", "issuer", "note"]


def list_rewards(kind="", year=None, employee_id=None) -> list[dict]:
    sql = ("SELECT r.*, e.name, e.emp_no, e.department, e.rank FROM rewards r "
           "JOIN employees e ON e.id=r.employee_id WHERE 1=1")
    params = []
    if kind:
        sql += " AND r.kind=?"
        params.append(kind)
    if year:
        sql += " AND substr(r.action_date,1,4)=?"
        params.append(str(year))
    if employee_id:
        sql += " AND r.employee_id=?"
        params.append(employee_id)
    return query(sql + " ORDER BY r.action_date DESC, r.id DESC", params)


def save_reward(rid, data: dict) -> int:
    d = {k: (data.get(k) or None) for k in REWARD_FIELDS}
    d["amount"] = enc(int(str(d["amount"] or 0).replace(",", "")))
    for k in ("reason", "note"):
        d[k] = enc(d[k])
    if rid:
        sets = ", ".join(f"{k}=?" for k in d)
        execute(f"UPDATE rewards SET {sets} WHERE id=?", list(d.values()) + [rid])
        return rid
    cols = ", ".join(d.keys())
    return execute(f"INSERT INTO rewards ({cols}) VALUES ({', '.join('?' * len(d))})", list(d.values()))


def delete_reward(rid: int):
    execute("DELETE FROM rewards WHERE id=?", (rid,))


# ════════════════════════════════════════════════════════════════════════
# 근로특이사항
# ════════════════════════════════════════════════════════════════════════
SPECIAL_FIELDS = ["employee_id", "category", "start_date", "end_date", "actual_end",
                  "reduced_hours", "child_name", "child_birth", "replacement", "subsidy", "note"]


def list_specials(category="", status="", employee_id=None) -> list[dict]:
    sql = ("SELECT ws.*, e.name, e.emp_no, e.department, e.rank, e.gender FROM work_specials ws "
           "JOIN employees e ON e.id=ws.employee_id WHERE 1=1")
    params = []
    if category:
        sql += " AND ws.category=?"
        params.append(category)
    if employee_id:
        sql += " AND ws.employee_id=?"
        params.append(employee_id)
    rows = query(sql + " ORDER BY ws.start_date DESC, ws.id DESC", params)
    for r in rows:
        r["status"] = _special_status(r)
        s, e = to_date(r["start_date"]), to_date(r["actual_end"]) or to_date(r["end_date"])
        r["days"] = (e - s).days + 1 if s and e else None
    if status:
        rows = [r for r in rows if r["status"] == status]
    return rows


def save_special(sid, data: dict) -> int:
    d = {k: (data.get(k) or None) for k in SPECIAL_FIELDS}
    d["reduced_hours"] = float(d["reduced_hours"]) if d["reduced_hours"] else None
    category, emp_id, start = d["category"], d["employee_id"], d["start_date"]
    for k in ("child_name", "child_birth", "note"):
        d[k] = enc(d[k])
    if sid:
        sets = ", ".join(f"{k}=?" for k in d)
        execute(f"UPDATE work_specials SET {sets} WHERE id=?", list(d.values()) + [sid])
        return sid
    cols = ", ".join(d.keys())
    new_id = execute(f"INSERT INTO work_specials ({cols}) VALUES ({', '.join('?' * len(d))})",
                     list(d.values()))
    if category in LEAVE_CATEGORIES and category not in ("배우자 출산휴가", "난임치료휴가"):
        add_appointment(emp_id, start, "휴직", None, category, "근로특이사항 등록")
    return new_id


def delete_special(sid: int):
    execute("DELETE FROM work_specials WHERE id=?", (sid,))


# ════════════════════════════════════════════════════════════════════════
# 설정 / 코드
# ════════════════════════════════════════════════════════════════════════
def list_codes_all() -> list[dict]:
    return query("SELECT * FROM codes ORDER BY group_code, sort_order, id")


def code_group(cid: int) -> str | None:
    code = query_one("SELECT group_code FROM codes WHERE id=?", (cid,))
    return code["group_code"] if code else None


def add_code(group, name):
    with tx() as conn:
        mx = conn.execute("SELECT COALESCE(MAX(sort_order),0)+1 FROM codes WHERE group_code=?",
                          (group,)).fetchone()[0]
        conn.execute("INSERT OR IGNORE INTO codes(group_code, name, sort_order) VALUES (?,?,?)",
                     (group, name, mx))


def delete_code(cid: int):
    execute("DELETE FROM codes WHERE id=?", (cid,))


# 구분값 이름을 바꿀 때 함께 고칠 자료 (테이블, 컬럼)
CODE_USAGE = {
    "DEPT": [("employees", "department")], "RANK": [("employees", "rank")], "TITLE": [("employees", "title")],
    "LOCATION": [("employees", "work_location")], "EMPLOYMENT_TYPE": [("employees", "employment_type")],
    "EDU_LEVEL": [("employees", "edu_level")], "APPOINTMENT_TYPE": [("appointments", "appt_type")],
    "SALARY_REASON": [("salaries", "reason")], "REWARD_포상": [("rewards", "category")], "REWARD_징계": [("rewards", "category")],
    "ASSET_EVENT": [("ga_asset_history", "event_type")], "ASSET_LOC": [("ga_assets", "location")],
    "FUEL": [("ga_vehicles", "fuel")], "VEHICLE_OWNERSHIP": [("ga_vehicles", "ownership")],
    "MAINT_CATEGORY": [("ga_vehicle_maint", "category")], "DOC_METHOD": [("ga_docs", "method")],
    "RETENTION": [("ga_docs", "retention")], "SEAL_TYPE": [("ga_seal_logs", "seal_type")],
    "GA_CONTRACT": [("ga_contracts", "category")],
    "EVENT_CATEGORY": [("ga_events", "category"), ("ga_event_rules", "category")],
    "EVENT_RELATION": [("ga_events", "relation"), ("ga_event_rules", "relation")],
}


def rename_code(cid: int, name: str) -> bool:
    """이름 변경. 같은 그룹에 이미 있으면 False."""
    code = query_one("SELECT * FROM codes WHERE id=?", (cid,))
    if not code or query_one("SELECT 1 x FROM codes WHERE group_code=? AND name=? AND id!=?", (code["group_code"], name, cid)):
        return False
    with tx() as conn:
        conn.execute("UPDATE codes SET name=? WHERE id=?", (name, cid))
        if code["group_code"] == "ONBOARD" or code["group_code"] == "OFFBOARD":
            conn.execute("UPDATE employee_checks SET item=? WHERE kind=? AND item=?", (name, code["group_code"], code["name"]))
        for table, col in CODE_USAGE.get(code["group_code"], []):
            conn.execute(f"UPDATE {table} SET {col}=? WHERE {col}=?", (name, code["name"]))
    audit("system", "구분값 변경", code["group_code"], f"{code['name']} → {name}")
    return True


def move_code(cid: int, direction: int):
    """같은 그룹 안에서 앞뒤 항목과 순서를 바꿉니다."""
    code = query_one("SELECT * FROM codes WHERE id=?", (cid,))
    if not code:
        return
    rows = query("SELECT id FROM codes WHERE group_code=? ORDER BY sort_order, id", (code["group_code"],))
    ids = [r["id"] for r in rows]
    i = ids.index(cid)
    j = i + (1 if direction > 0 else -1)
    if 0 <= j < len(ids):
        ids[i], ids[j] = ids[j], ids[i]
        with tx() as conn:
            for order, rid in enumerate(ids):
                conn.execute("UPDATE codes SET sort_order=? WHERE id=?", (order, rid))


def set_code_behavior(cid: int, behavior: str):
    execute("UPDATE codes SET behavior=? WHERE id=?", (behavior or None, cid))


def recent_audit(limit=30, q="", date_from="", date_to="") -> list[dict]:
    sql = "SELECT * FROM audit_logs WHERE 1=1"
    params = []
    if q:
        sql += " AND (username LIKE ? OR action LIKE ? OR target LIKE ? OR ip LIKE ?)"
        params += [f"%{q}%"] * 4
    if date_from:
        sql += " AND created_at >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND created_at < date(?, '+1 day')"
        params.append(date_to)
    return query(sql + " ORDER BY id DESC LIMIT ?", params + [limit])
