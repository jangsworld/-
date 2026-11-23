"""이카운트 사원등록 로봇과 주고받는 일감 (BMS 쪽).

로봇은 관리 PC 에서 돌며 이카운트 전용 계정으로 사원등록 화면에 값을 넣고 저장한 뒤, 다시 열어 대조한 결과를 돌려줍니다.
- 로봇은 BMS 에 로그인하지 않습니다. 설정에서 만든 '로봇 열쇠'(무작위, BMS 에는 지문만 저장)로만 통합니다.
- 로봇이 받아 갈 수 있는 것은 담당자가 [이카운트에 넣기]를 누른 직원 한 명의 값뿐입니다 (목록 조회 없음).
- 이카운트 비밀번호는 BMS 에 저장하지 않습니다 (관리 PC 에 암호화 보관).
- 회사가 둘이면 로봇도 회사마다 따로입니다 (이카운트 회사코드·계정·열쇠·부서 짝이 다름). 열쇠가 어느 회사 것인지로
  로봇의 회사를 정하고, 그 회사 직원의 일감만 내줍니다. 설정 이름은 자회사 것에 co2_ 가 붙습니다.
"""
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta

from app import company
from app.db import audit, execute, get_settings, query, query_one, set_setting, tx
from app.modules.hr import ecount

OPEN = ("요청", "처리중")
STALE_MIN = 20                    # 로봇이 가져간 뒤 이 시간 안에 결과가 없으면 실패로
DEFAULT_OK_CONFIRMS = "사업자단위과세"
SEEN_FRESH_MIN = 10               # 이 시간 안에 로봇이 다녀갔으면 '켜져 있음'


class RobotError(ValueError):
    pass


def _changed(sql: str, params=()) -> int:
    """UPDATE 가 실제로 바꾼 줄 수 (두 요청이 동시에 와도 한쪽만 성공하게)."""
    with tx() as conn:
        return conn.execute(sql, params).rowcount


def _hash(key: str) -> str:
    return hashlib.sha256(("bms-ecount-robot:" + key).encode()).hexdigest()


def _k(name: str, co: str = "1") -> str:
    return company.key(name, company.norm(co))


def new_key(co: str = "1") -> str:
    """새 로봇 열쇠. 화면에 한 번만 보여 주고 BMS 에는 지문만 남김 (예전 열쇠는 바로 못 씀)."""
    key = secrets.token_urlsafe(32)
    set_setting(_k("ecount_robot_key", co), _hash(key))
    audit("hr", "이카운트 로봇 열쇠 새로 만듦", company.short(company.norm(co)) if company.enabled() else "", "")
    return key


def key_company(key: str | None) -> str:
    """열쇠가 맞는 회사 ('1' / '2'), 어느 것도 아니면 빈 글자. 두 회사 열쇠를 모두 견주어 걸리는 시간이 같게."""
    s = get_settings()
    h = _hash(str(key or ""))
    found = ""
    for co in company.IDS:
        saved = s.get(_k("ecount_robot_key", co)) or ""
        if hmac.compare_digest(saved or "-", h) and saved and key:
            found = found or co
    return found


def check_key(key: str | None) -> bool:
    return bool(key_company(key))


def touch(co: str = "1"):
    set_setting(_k("ecount_robot_seen", co), datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


def status(co: str = "1") -> dict:
    s = get_settings()
    seen = s.get(_k("ecount_robot_seen", co)) or ""
    fresh = False
    try:
        fresh = bool(seen) and datetime.now() - datetime.strptime(seen, "%Y-%m-%d %H:%M:%S") < timedelta(minutes=SEEN_FRESH_MIN)
    except ValueError:
        pass
    return {"has_key": bool(s.get(_k("ecount_robot_key", co))), "seen": seen, "online": fresh}


# ── 설정: 부서 짝 · 지급구분 · 확인 창 ──
def ecount_depts(co: str = "1") -> list[list[str]]:
    """로봇이 이카운트에서 읽어 온 부서 목록 [[코드, 이름], ...]"""
    try:
        return [x for x in json.loads(get_settings().get(_k("ecount_depts", co)) or "[]") if isinstance(x, list) and len(x) == 2]
    except ValueError:
        return []


def save_ecount_depts(rows, co: str = "1") -> int:
    clean = []
    for x in rows or []:
        if isinstance(x, (list, tuple)) and len(x) == 2 and str(x[0]).strip() and len(str(x[0])) <= 30 and len(str(x[1])) <= 60:
            clean.append([str(x[0]).strip(), str(x[1]).strip()])
    if clean:
        set_setting(_k("ecount_depts", co), json.dumps(clean[:300], ensure_ascii=False))
    return len(clean)


def dept_map(co: str = "1") -> dict[str, str]:
    try:
        m = json.loads(get_settings().get(_k("ecount_dept_map", co)) or "{}")
        return {str(k): str(v) for k, v in m.items() if v}
    except ValueError:
        return {}


def save_dept_map(m: dict, co: str = "1"):
    valid = {c for c, _n in ecount_depts(co)}
    clean = {str(k): str(v) for k, v in m.items() if v and (not valid or str(v) in valid)}
    set_setting(_k("ecount_dept_map", co), json.dumps(clean, ensure_ascii=False))
    audit("hr", "이카운트 부서 짝 저장", f"{len(clean)}개", "")


def dept_code_for(department: str, co: str = "1") -> str:
    """BMS 부서 → 이카운트 부서코드. 그 부서에 짝이 없으면 상위 부서의 짝을 씀."""
    m = dept_map(co)
    name = (department or "").strip()
    seen = set()
    while name and name not in seen:
        if m.get(name):
            return m[name]
        seen.add(name)
        row = query_one("SELECT p.name FROM codes c JOIN codes p ON p.id=c.parent_id "
                        "WHERE c.group_code='DEPT' AND c.name=?", (name,))
        name = row["name"] if row else ""
    return ""


def pay_std(co: str = "1") -> str:
    return get_settings().get(_k("ecount_pay_std", co)) or "00001"


def ok_confirms(co: str = "1") -> list[str]:
    raw = get_settings().get(_k("ecount_ok_confirms", co))
    raw = DEFAULT_OK_CONFIRMS if raw is None else raw
    return [x.strip() for x in raw.split(",") if x.strip()]


# ── 일감 ──
def _expire():
    limit = (datetime.now() - timedelta(minutes=STALE_MIN)).strftime("%Y-%m-%d %H:%M:%S")
    execute("UPDATE ecount_robot_jobs SET status='실패', finished_at=datetime('now','localtime'), "
            "message='로봇이 결과를 돌려주지 않았습니다 (시간 초과). 이카운트에서 직접 확인하세요.' "
            "WHERE status='처리중' AND claimed_at < ?", (limit,))


def latest_jobs() -> dict[tuple[int, str], dict]:
    """직원·구분별 가장 최근 일감."""
    _expire()
    out = {}
    for r in query("SELECT * FROM ecount_robot_jobs ORDER BY id"):
        try:
            r["detail"] = json.loads(r.get("detail") or "{}")
        except ValueError:
            r["detail"] = {}
        out[(r["employee_id"], r["action"])] = r
    return out


def request(keys: list[tuple[int, str]], by: str = "") -> int:
    """담당자가 [이카운트에 넣기]: 반영 대기 중인 직원만, 이미 요청·처리 중이면 건너뜀."""
    s = get_settings()
    has = {co: bool(s.get(_k("ecount_robot_key", co))) for co in company.IDS}
    pend = {(r["id"], r["action"]) for r in ecount.pending()}
    n = nokey = 0
    for emp_id, act in keys:
        if (emp_id, act) not in pend:
            continue
        if not has[company.of_employee(emp_id)]:              # 그 직원 회사의 로봇이 아직 연결되지 않음
            nokey += 1
            continue
        if query_one("SELECT 1 x FROM ecount_robot_jobs WHERE employee_id=? AND action=? AND status IN ('요청','처리중')", (emp_id, act)):
            continue
        execute("INSERT INTO ecount_robot_jobs(employee_id, action, status, requested_by) VALUES (?,?,?,?)", (emp_id, act, "요청", by))
        n += 1
    if n:
        audit("hr", "이카운트 로봇 요청", f"{n}건", by)
    if nokey and not n:
        raise RobotError("로봇 열쇠가 아직 없습니다. 「로봇」 탭에서 먼저 연결하세요.")
    return n


def cancel(job_id: int, by: str = "") -> bool:
    return bool(_changed("UPDATE ecount_robot_jobs SET status='취소', finished_at=datetime('now','localtime'), message=? "
                         "WHERE id=? AND status='요청'", (f"{by} 취소", job_id)))


def payload(emp_id: int, action: str) -> dict:
    v = ecount.values(emp_id, include_rrn=(action == "신규"))
    if not v:
        return {}
    if action == "퇴사":
        return {"emp_no": v["emp_no"], "name": v["name"], "resign_date": v["resign_date"], "resign_reason": v.get("resign_reason") or ""}
    items = {k[4:]: int(val) for k, val in v.items() if k.startswith("pay:") and str(val).isdigit() and int(val) > 0}
    sal = query_one("SELECT annual_salary FROM salaries WHERE employee_id=? ORDER BY year DESC LIMIT 1", (emp_id,))
    out = {k: v.get(k) or "" for k in ("emp_no", "name", "rrn", "email", "phone", "address", "bank_name", "bank_account", "account_holder",
                                       "hire_date", "birth_date", "rank", "title", "department", "householder", "dependents",
                                       "children_8_20", "insured_monthly", "pension_monthly", "employ_monthly")}
    co = company.of_employee(emp_id)
    out.update(dept_code=dept_code_for(v.get("department") or "", co), pay_std_code=pay_std(co), pay_items=items,
               annual_salary=(sal or {}).get("annual_salary") or "")
    return out


def claim(co: str = "1") -> dict | None:
    """로봇이 자기 회사 직원의 다음 일감 하나를 가져감 (요청 → 처리중). 없으면 None."""
    co = company.norm(co)
    touch(co)
    _expire()
    job = query_one("SELECT j.* FROM ecount_robot_jobs j JOIN employees e ON e.id=j.employee_id "
                    "WHERE j.status='요청'" + company.sql("e", co) + " ORDER BY j.id LIMIT 1")
    if not job:
        return None
    if not _changed("UPDATE ecount_robot_jobs SET status='처리중', claimed_at=datetime('now','localtime') WHERE id=? AND status='요청'",
                    (job["id"],)):
        return None
    data = payload(job["employee_id"], job["action"])
    if not data:
        finish(job["id"], False, "BMS 에서 직원을 찾을 수 없습니다", {})
        return None
    audit("hr", "개인정보 내보내기", f"이카운트 로봇 {data['emp_no']} {data['name']}",
          job["action"] + (", 주민번호 포함" if data.get("rrn") else ""))
    return {"job_id": job["id"], "action": job["action"], "values": data, "ok_confirms": ok_confirms(co)}


def finish(job_id: int, ok: bool, message: str, detail: dict, co: str | None = None) -> bool:
    """로봇의 결과. 저장되고 대조까지 맞으면 '완료'(반영 기록에도 남김), 아니면 '실패'. co 를 주면 그 회사 일감만."""
    job = query_one("SELECT * FROM ecount_robot_jobs WHERE id=? AND status='처리중'", (job_id,))
    if not job:
        return False
    if co is not None and company.of_employee(job["employee_id"]) != company.norm(co):
        return False
    clean = {"skipped": [[str(a)[:40], str(b)[:120]] for a, b in (detail.get("skipped") or [])[:40]],
             "mismatch": [str(x)[:40] for x in (detail.get("mismatch") or [])[:40]],
             "confirmed": [str(x)[:120] for x in (detail.get("confirmed") or [])[:5]],
             "saved": bool(detail.get("saved")), "existed": bool(detail.get("existed"))}
    good = bool(ok) and clean["saved"] and not clean["mismatch"]
    execute("UPDATE ecount_robot_jobs SET status=?, finished_at=datetime('now','localtime'), message=?, detail=? WHERE id=?",
            ("완료" if good else "실패", str(message or "")[:300], json.dumps(clean, ensure_ascii=False), job_id))
    if good:
        note = "로봇 저장·대조 완료" + (" · 못 넣은 칸: " + ", ".join(a for a, _b in clean["skipped"]) if clean["skipped"] else "")
        ecount.mark([(job["employee_id"], job["action"])], "완료", "로봇", note[:200])
    audit("hr", "이카운트 로봇 결과", f"emp#{job['employee_id']} {job['action']}", "완료" if good else f"실패: {str(message)[:80]}")
    return True
