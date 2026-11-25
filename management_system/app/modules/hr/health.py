"""건강검진 관리: 누가 올해 검진을 받았는지, 안 받은 사람이 누구인지.

- 검진 결과(판정·소견)는 건강에 관한 민감정보라 받지 않습니다. 수검 여부·검진일·기관·구분만 기록합니다.
  꼭 필요한 메모(예: 재검 예정일)는 암호화해 저장합니다.
- 일반 건강검진은 사무직 2년에 1회, 그 밖은 1년에 1회가 원칙입니다(산업안전보건법). 직원마다 주기를 정해 두면
  올해 대상인지 자동으로 가립니다: 2년 주기는 지난해에 받았으면 올해는 대상이 아닙니다.
- 관리자·인사담당만 봅니다 (middleware RULES /hr/health).
"""
from app.common import to_date, today
from app.db import audit, batch, current_actor, execute, get_settings, query, query_one, set_setting
from app import company
from app.security.crypto import enc

KINDS = ("일반", "특수", "배치 전", "채용", "종합")
CYCLES = {"1": "매년", "2": "2년에 1회"}


class HealthError(ValueError):
    pass


def cycles() -> dict[int, str]:
    """직원별 주기 (정하지 않은 사람은 기본값)."""
    return {r["employee_id"]: r["cycle"] for r in query("SELECT employee_id, cycle FROM health_cycles")}


def default_cycle() -> str:
    v = get_settings().get("health_default_cycle") or "2"
    return v if v in CYCLES else "2"


def set_default_cycle(v: str):
    if v not in CYCLES:
        raise HealthError("주기를 고르세요")
    set_setting("health_default_cycle", v)
    audit("hr", "건강검진 기본 주기 변경", "", CYCLES[v])


def set_cycle(emp_ids: list[int], cycle: str) -> int:
    """cycle: '1' / '2' / '' (기본값 따름)."""
    if cycle not in CYCLES and cycle != "":
        raise HealthError("주기를 고르세요")
    with batch():
        for eid in emp_ids:
            if cycle:
                execute("INSERT INTO health_cycles(employee_id, cycle) VALUES (?,?) ON CONFLICT(employee_id) DO UPDATE SET cycle=excluded.cycle",
                        (eid, cycle))
            else:
                execute("DELETE FROM health_cycles WHERE employee_id=?", (eid,))
        audit("hr", "건강검진 주기 변경", f"{len(emp_ids)}명", CYCLES.get(cycle, "기본값"))
    return len(emp_ids)


def records(year: int | None = None, employee_id: int | None = None) -> list[dict]:
    sql = ("SELECT h.*, e.name, e.department, e.rank FROM health_checks h JOIN employees e ON e.id=h.employee_id WHERE 1=1" + ("" if employee_id else company.sql("e")))
    p = []
    if year:
        sql += " AND substr(h.check_date,1,4)=?"
        p.append(str(year))
    if employee_id:
        sql += " AND h.employee_id=?"
        p.append(employee_id)
    return query(sql + " ORDER BY h.check_date DESC, h.id DESC", p)


def save(f: dict) -> int:
    """한 건 저장(수정은 id). 여러 명을 한꺼번에 넣을 때는 employee_ids."""
    d = to_date(f.get("check_date"))
    if not d:
        raise HealthError("검진일을 입력하세요")
    if d > today():
        raise HealthError("검진일은 오늘보다 뒤일 수 없습니다 (받은 뒤에 등록하세요)")
    kind = (f.get("kind") or "일반").strip()
    if kind not in KINDS:
        raise HealthError("검진 구분을 고르세요")
    place = (f.get("institution") or "").strip()[:100] or None
    note = (f.get("note") or "").strip()[:300]
    ids = [int(x) for x in (f.get("employee_ids") or []) if str(x).isdigit()]
    rid = int(f.get("id") or 0)
    with batch():
        if rid:
            if not query_one("SELECT 1 x FROM health_checks WHERE id=?", (rid,)):
                raise HealthError("기록을 찾을 수 없습니다")
            execute("UPDATE health_checks SET check_date=?, kind=?, institution=?, note=? WHERE id=?",
                    (d.isoformat(), kind, place, enc(note) if note else None, rid))
            audit("hr", "건강검진 기록 수정", f"#{rid}", f"{d.isoformat()} {kind}")
            return 1
        if not ids:
            raise HealthError("직원을 고르세요")
        n = 0
        for eid in ids:
            if query_one("SELECT 1 x FROM health_checks WHERE employee_id=? AND check_date=? AND kind=?", (eid, d.isoformat(), kind)):
                continue                                   # 같은 날 같은 검진은 한 번만
            execute("INSERT INTO health_checks(employee_id, check_date, kind, institution, note, created_by) VALUES (?,?,?,?,?,?)",
                    (eid, d.isoformat(), kind, place, enc(note) if note else None, current_actor.get()[1]))
            n += 1
        audit("hr", "건강검진 기록 등록", f"{n}명", f"{d.isoformat()} {kind}")
    return n


def delete(rid: int):
    from app import trash
    trash.remove("health_checks", rid, audit_note=("hr", "건강검진 기록 삭제", f"#{rid}", "휴지통"))


def overview(year: int, department: str = "", q: str = "") -> dict:
    """그 해 재직자별 일반 검진 수검 현황. status: done(받음) / todo(대상, 아직) / skip(올해 대상 아님) / new(올해 입사, 채용 검진으로 갈음 가능)."""
    from app.db import dept_scope
    cyc, dflt = cycles(), default_cycle()
    scope = (set(dept_scope(department)) | {department}) if department else None
    q = (q or "").strip()
    by = {}
    for r in query("SELECT * FROM health_checks WHERE substr(check_date,1,4) IN (?,?) ORDER BY check_date", (str(year), str(year - 1))):
        by.setdefault((r["employee_id"], r["check_date"][:4]), []).append(r)
    rows = []
    for e in query("SELECT id, emp_no, name, department, rank, hire_date, resign_date FROM employees "
                   "WHERE hire_date<=? AND (resign_date IS NULL OR resign_date='' OR resign_date>?)" + company.sql("") + " ORDER BY department, name",
                   (f"{year}-12-31", today().isoformat() if year >= today().year else f"{year}-12-31")):
        if scope is not None and (e["department"] or "") not in scope:
            continue
        if q and q not in (e["name"] or ""):
            continue
        c = cyc.get(e["id"]) or dflt
        mine = by.get((e["id"], str(year)), [])
        prev = by.get((e["id"], str(year - 1)), [])
        general = [r for r in mine if r["kind"] in ("일반", "종합", "채용")]
        last = max((r["check_date"] for r in mine + prev), default="")
        if general:
            status = "done"
        elif c == "2" and any(r["kind"] in ("일반", "종합", "채용") for r in prev):
            status = "skip"
        elif (e["hire_date"] or "")[:4] == str(year):
            status = "new"
        else:
            status = "todo"
        rows.append({"emp": e, "cycle": c, "cycle_custom": e["id"] in cyc, "status": status, "last": last, "records": mine})
    count = {k: sum(1 for r in rows if r["status"] == k) for k in ("done", "todo", "skip", "new")}
    need = count["done"] + count["todo"]
    return {"rows": rows, "count": count, "rate": round(count["done"] / need * 100) if need else None}
