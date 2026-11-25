"""휴지통: 지우기 → 되살리기가 원래와 똑같은지, 권한·자동 완전삭제·NAS 무관, 실제 삭제 단추가 모두 휴지통으로 가는지."""
import os
import re
import sqlite3
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import archive, trash
from app.db import execute, query, query_one, set_setting
from app.main import app
from app.security import auth
from app.security.crypto import enc

O = {"origin": "http://testserver"}
PW = "Tr-Strong-Pass-1!"
SKIP = {"audit_logs", "trash_items", "sqlite_sequence", "sessions", "login_attempts", "mfa_devices", "mfa_state", "settings", "archive_folders"}


def d(n):
    return (date.today() + timedelta(days=n)).isoformat()


def dump():
    """DB 전체(기록용 표 제외)를 그대로 읽은 것."""
    con = sqlite3.connect(os.environ["BMS_DB_PATH"])
    try:
        out = {}
        for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
            if t not in SKIP:
                out[t] = sorted(con.execute(f"SELECT * FROM {t}").fetchall(), key=repr)
        return out
    finally:
        con.close()


def diff(a, b):
    return {t: ([x for x in a[t] if x not in b[t]][:2], [x for x in b[t] if x not in a[t]][:2]) for t in a if a[t] != b[t]}


def login(c, name):
    c.post("/login", data={"username": name, "password": PW}, headers=O)
    return re.search(r'name="csrf-token" content="([^"]*)"', c.get("/account").text).group(1)


def user(name, role):
    if not query_one("SELECT 1 FROM users WHERE username=?", (name,)):
        auth.create_user(name, name, role, PW, must_change=False)


@pytest.fixture(scope="module", autouse=True)
def _started():
    with TestClient(app):
        for n, r in (("tr_admin", "admin"), ("tr_hr", "hr"), ("tr_ga", "ga"), ("tr_view", "viewer")):
            user(n, r)
        yield


@pytest.fixture(scope="module")
def made():
    """자료 종류마다 한 건씩 (딸린 기록 포함)."""
    e = execute("INSERT INTO employees(emp_no, name, hire_date, department) VALUES ('TR-1','휴지통직원',?, '재무팀')", (d(-900),))
    e2 = execute("INSERT INTO employees(emp_no, name, hire_date) VALUES ('TR-2','다른직원',?)", (d(-900),))
    v = execute("INSERT INTO ga_vehicles(plate_no, model, manager_id) VALUES ('99가9999','시험차',?)", (e,))
    a = execute("INSERT INTO ga_assets(asset_no, category, name, holder_id) VALUES ('TR-A1','PC','시험자산',?)", (e,))
    t = execute("INSERT INTO doc_templates(category, name, content) VALUES ('정규직','휴지통양식','<p>x</p>')")
    co = query_one("SELECT id FROM training_courses LIMIT 1")["id"]
    m = {"employees": e, "ga_vehicles": v, "ga_assets": a, "doc_templates": t}
    execute("INSERT INTO employee_rrn(employee_id, rrn) VALUES (?,?)", (e, enc("900101-1234567"))) if _has("employee_rrn", "rrn") else None
    m["hr_documents"] = execute("INSERT INTO hr_documents(employee_id, template_id, category, title, content, annual_salary) VALUES (?,?,'정규직','휴지통계약',?,?)", (e, t, enc("<p>본문</p>"), enc(50000000)))
    execute("INSERT INTO hr_doc_mails(doc_id, target, to_addr) VALUES (?,'직접입력',?)", (m["hr_documents"], enc("a@b.example")))
    m["appointments"] = execute("INSERT INTO appointments(employee_id, appt_date, appt_type, to_value) VALUES (?,?,'승진','과장')", (e, d(-10)))
    m["salaries"] = execute("INSERT INTO salaries(employee_id, year, annual_salary, reason) VALUES (?,2024,?, '정기인상')", (e, enc(41000000)))
    m["salary_steps"] = execute("INSERT INTO salary_steps(employee_id, year, effective_date, annual_salary) VALUES (?,2024,'2024-01-01',?)", (e, enc(40000000)))
    m["rewards"] = execute("INSERT INTO rewards(employee_id, kind, category, action_date, amount, reason) VALUES (?,'포상','우수사원',?,?,'시험')", (e, d(-5), enc(100000)))
    m["work_specials"] = execute("INSERT INTO work_specials(employee_id, category, start_date, end_date, note) VALUES (?,'육아휴직',?,?,'메모')", (e, d(-5), d(50)))
    m["health_checks"] = execute("INSERT INTO health_checks(employee_id, check_date, note) VALUES (?,?,?)", (e, d(-9), enc("비고")))
    m["training_records"] = execute("INSERT INTO training_records(course_id, employee_id, edu_date) VALUES (?,?,?)", (co, e, d(-9)))
    m["dual_enrollments"] = execute("INSERT INTO dual_enrollments(employee_id, workplace, start_date, our_before) VALUES (?,'상대',?,?)", (e, d(-9), enc(3000000)))
    m["employee_insurances"] = execute("INSERT INTO employee_insurances(employee_id, product) VALUES (?,'공제')", (e,))
    for x in ("off", "on"):
        execute(f"INSERT INTO {x}boardings(employee_id, last_day) VALUES (?,?)", (e, d(30)))
        execute(f"INSERT INTO {x}board_checks(employee_id, key) VALUES (?,'a1')", (e,))
        execute(f"INSERT INTO {x}board_mails(employee_id, how, body) VALUES (?,'직접 전달',?)", (e, enc("본문")))
        m[f"{x}boardings"] = e
    m["incentive_payments"] = execute("INSERT INTO incentive_payments(year, pay_date, department, employee_id, amount) VALUES (2026,?,'재무팀',?,?)", (d(-3), e, enc(500000)))
    m["compliance_tasks"] = execute("INSERT INTO compliance_tasks(title, month, day) VALUES ('휴지통의무',3,31)")
    execute("INSERT INTO compliance_done(task_id, period, done_date) VALUES (?,'2026',?)", (m["compliance_tasks"], d(-2)))
    m["ga_contracts"] = execute("INSERT INTO ga_contracts(category, title, counterparty, status, manager_id) VALUES ('임대차','휴지통계약건','상대','유효',?)", (e,))
    m["ga_company_items"] = execute("INSERT INTO ga_company_items(category, name, expire_date) VALUES ('인증','휴지통인증',?)", (d(90),))
    execute("INSERT INTO ga_company_history(item_id, done_date, new_expire) VALUES (?,?,?)", (m["ga_company_items"], d(-100), d(90)))
    m["ga_docs"] = execute("INSERT INTO ga_docs(direction, doc_no, doc_date, counterpart, title, status, handler_id) VALUES ('수신','TR-D1',?,'상대','휴지통문서','접수',?)", (d(-2), e))
    m["ga_seal_logs"] = execute("INSERT INTO ga_seal_logs(use_date, seal_type, purpose, status, requester_id) VALUES (?,'법인인감','시험','신청',?)", (d(-1), e))
    m["ga_events"] = execute("INSERT INTO ga_events(employee_id, event_date, category, relation, amount, detail) VALUES (?,?,'결혼','본인',1000,?)", (e, d(-1), enc("장소")))
    m["ga_event_rules"] = execute("INSERT INTO ga_event_rules(category, relation, amount) VALUES ('시험구분','시험대상',1)")
    m["ga_asset_history"] = execute("INSERT INTO ga_asset_history(asset_id, event_date, event_type, holder_id) VALUES (?,?,'지급',?)", (a, d(-1), e))
    m["ga_vehicle_logs"] = execute("INSERT INTO ga_vehicle_logs(vehicle_id, drive_date, driver_id, km_start, km_end, destination) VALUES (?,?,?,1,9,?)", (v, d(-1), e, enc("서울")))
    m["ga_vehicle_maint"] = execute("INSERT INTO ga_vehicle_maint(vehicle_id, maint_date, category) VALUES (?,?,'정비')", (v, d(-1)))
    m["ga_vehicle_fines"] = execute("INSERT INTO ga_vehicle_fines(vehicle_id, violation_at, kind, amount, driver_id) VALUES (?,?,'과태료',1000,?)", (v, d(-1) + " 10:00", e))
    m["ga_vehicle_damages"] = execute("INSERT INTO ga_vehicle_damages(vehicle_id, occur_date, kind, driver_id) VALUES (?,?,'파손',?)", (v, d(-1), e))
    m["ga_vehicle_inspections"] = execute("INSERT INTO ga_vehicle_inspections(vehicle_id, kind, insp_date, to_emp_id) VALUES (?,'정기점검',?,?)", (v, d(-1), e))
    execute("INSERT INTO ga_vehicle_photos(inspection_id, slot, size, sha256) VALUES (?,'front',1,'x')", (m["ga_vehicle_inspections"],))
    m["codes"] = execute("INSERT INTO codes(group_code, name) VALUES ('RETENTION','휴지통구분')")
    execute("INSERT INTO ga_vehicle_assign(vehicle_id, employee_id, start_date) VALUES (?,?,?)", (v, e, d(-5)))
    m["_e2"] = e2
    return m


def _has(t, c):
    con = sqlite3.connect(os.environ["BMS_DB_PATH"])
    try:
        return c in [r[1] for r in con.execute(f"PRAGMA table_info({t})")]
    finally:
        con.close()


def rm(table, rid):
    if table in ("offboardings", "onboardings"):
        p = table[:-3].replace("boarding", "board")
        p = "offboard" if table.startswith("off") else "onboard"
        return trash.remove(table, rid, pk="employee_id", extra=((p + "_checks", "employee_id", rid), (p + "_mails", "employee_id", rid)))
    return trash.remove(table, rid)


def test_every_kind_roundtrip(made):
    """모든 종류: 지우면 사라지고, 되살리면 DB 전체가 지우기 전과 똑같음 (딸린 기록·암호문·비워진 담당자 칸까지)."""
    assert set(trash.META) <= set(made), set(trash.META) - set(made)
    for table in trash.META:
        before = dump()
        tid = rm(table, made[table])
        assert tid and diff(before, dump()).get(table), table
        raw = sqlite3.connect(os.environ["BMS_DB_PATH"]).execute("SELECT data FROM trash_items WHERE id=?", (tid,)).fetchone()[0]
        assert raw.startswith("enc:v1:") and "휴지통" not in raw                        # 담아 둔 자료는 암호화
        it = next(x for x in trash.items("admin") if x["id"] == tid)
        assert it["title"] and trash.detail(tid, "admin"), table
        msg, url = trash.restore(tid, "admin")
        assert "되살렸습니다" in msg and "뺐습니다" not in msg, (table, msg)
        assert diff(before, dump()) == {}, (table, diff(before, dump()))
        assert bool(url) == bool(trash.META[table][5]), table
        with pytest.raises(trash.TrashError):
            trash.restore(tid, "admin")                                                  # 두 번은 안 됨
    it = trash.remove("employees", made["employees"])
    kids = next(x for x in trash.items("admin") if x["id"] == it)["kids"]
    assert kids.get("연봉") == 1 and kids.get("근로특이사항") == 1 and kids.get("퇴사 절차") == 1
    trash.restore(it, "hr")


def test_no_salary_or_rrn_in_list(made):
    tid = rm("salaries", made["salaries"])
    text = str(trash.detail(tid, "admin")) + str(trash.items("admin"))
    assert "41000000" not in text and "900101" not in text
    trash.restore(tid, "admin")


def test_missing_parent_and_conflict(made):
    s = rm("work_specials", made["work_specials"])
    e = rm("employees", made["employees"])
    with pytest.raises(trash.TrashError, match="직원을 먼저"):
        trash.restore(s, "hr")
    assert query_one("SELECT 1 FROM trash_items WHERE id=?", (s,))                        # 실패해도 휴지통에 그대로
    trash.restore(e, "hr")
    trash.restore(s, "hr")
    assert query_one("SELECT 1 FROM work_specials WHERE id=?", (made["work_specials"],))
    # 같은 해 연봉을 새로 넣었으면 되살리지 않음
    t = rm("salaries", made["salaries"])
    execute("DELETE FROM salaries WHERE employee_id=? AND year=2024", (made["employees"],))
    n = execute("INSERT INTO salaries(employee_id, year, annual_salary) VALUES (?,2024,?)", (made["employees"], enc(1)))
    before = dump()
    with pytest.raises(trash.TrashError, match="이미 있어"):
        trash.restore(t, "hr")
    assert diff(before, dump()) == {}
    execute("DELETE FROM salaries WHERE id=?", (n,))
    trash.restore(t, "hr")
    # 사번을 다른 사람에게 다시 썼으면 직원을 되살리지 않고, 아무것도 반쯤 들어가지 않음
    e = rm("employees", made["employees"])
    x = execute("INSERT INTO employees(emp_no, name, hire_date) VALUES ('TR-1','새사람',?)", (d(0),))
    before = dump()
    with pytest.raises(trash.TrashError, match="이미 있어"):
        trash.restore(e, "hr")
    assert diff(before, dump()) == {}
    execute("DELETE FROM employees WHERE id=?", (x,))
    trash.restore(e, "hr")
    # 그 사이 담당자를 새로 정했으면 덮어쓰지 않음
    e = rm("employees", made["employees"])
    execute("UPDATE ga_contracts SET manager_id=? WHERE id=?", (made["_e2"], made["ga_contracts"]))
    trash.restore(e, "hr")
    assert query_one("SELECT manager_id m FROM ga_contracts WHERE id=?", (made["ga_contracts"],))["m"] == made["_e2"]
    execute("UPDATE ga_contracts SET manager_id=? WHERE id=?", (made["employees"], made["ga_contracts"]))
    # 담당자가 없어진 자료는 담당자 칸만 비우고 되살림
    c = rm("ga_contracts", made["ga_contracts"])
    e = rm("employees", made["employees"])
    trash.restore(c, "ga")
    assert query_one("SELECT manager_id m FROM ga_contracts WHERE id=?", (made["ga_contracts"],))["m"] is None
    trash.restore(e, "hr")
    execute("UPDATE ga_contracts SET manager_id=? WHERE id=?", (made["employees"], made["ga_contracts"]))


def test_certificate_number_and_planned_appointment(made):
    y = date.today().year
    e = made["employees"]
    no = f"제{y}-9001호"
    set_setting(f"cert_seq_{y}", "9001")
    c = execute("INSERT INTO hr_documents(doc_group, employee_id, category, title, doc_no, content) VALUES ('certificate',?,'재직증명','재직증명서',?,?)", (e, no, enc("x")))
    with TestClient(app) as cl:
        tok = login(cl, "tr_hr")
        cl.post(f"/hr/documents/{c}/delete", data={"csrf_token": tok}, headers=O)
    tid = query_one("SELECT id FROM trash_items WHERE table_name='hr_documents' AND root_id=?", (c,))["id"]
    c2 = execute("INSERT INTO hr_documents(doc_group, employee_id, category, title, doc_no, content) VALUES ('certificate',?,'재직증명','재직증명서',?,?)", (e, no, enc("y")))
    with pytest.raises(trash.TrashError, match="같은 번호"):
        trash.restore(tid, "hr")
    execute("DELETE FROM hr_documents WHERE id=?", (c2,))
    set_setting(f"cert_seq_{y}", "8000")
    trash.restore(tid, "hr")
    assert query_one("SELECT value v FROM settings WHERE key=?", (f"cert_seq_{y}",))["v"] == "9001"      # 되살린 번호를 다음 발급이 또 쓰지 않음
    execute("DELETE FROM hr_documents WHERE id=?", (c,))
    set_setting(f"cert_seq_{y}", "1")
    a = execute("INSERT INTO appointments(employee_id, appt_date, appt_type, to_value, field, status) VALUES (?,?,'승진','부장','rank','예정')", (e, d(-1)))
    tid = rm("appointments", a)
    msg, _ = trash.restore(tid, "hr")
    assert "이력" in msg and query_one("SELECT status FROM appointments WHERE id=?", (a,))["status"] == "이력"
    execute("DELETE FROM appointments WHERE id=?", (a,))


def test_roles(made):
    s, g, k = rm("work_specials", made["work_specials"]), rm("ga_docs", made["ga_docs"]), rm("codes", made["codes"])
    seen = {r: {x["id"] for x in trash.items(r)} for r in ("admin", "hr", "ga", "viewer")}
    assert {s, g, k} <= seen["admin"] and {s, g} <= seen["hr"] and k not in seen["hr"]
    assert g in seen["ga"] and s not in seen["ga"] and not seen["viewer"]
    for r, t in (("ga", s), ("hr", k), ("viewer", g)):
        for fn in (trash.restore, trash.detail):
            with pytest.raises(trash.TrashError):
                fn(t, r)
    for r in ("hr", "ga", "viewer", None):
        with pytest.raises(trash.TrashError):
            trash.purge(g, r)
    with TestClient(app) as c:
        assert c.get("/trash", follow_redirects=False).status_code == 303                # 로그인 전
    with TestClient(app) as c:
        tok = login(c, "tr_view")
        assert c.get("/trash").status_code == 403 and "/trash" not in c.get("/account").text
        assert c.post(f"/trash/{g}/restore", data={"csrf_token": tok}, headers=O).status_code == 403
    with TestClient(app) as c:
        tok = login(c, "tr_ga")
        page = c.get("/trash").text
        assert "휴지통문서" in page and "육아휴직" not in page and "/purge" not in page
        assert c.get(f"/trash/{s}/view").json()["ok"] is False
        assert c.post(f"/trash/{g}/purge", data={"csrf_token": tok}, headers=O).status_code == 403
        c.post(f"/trash/{s}/restore", data={"csrf_token": tok}, headers=O)
        assert not query_one("SELECT 1 FROM work_specials WHERE id=?", (made["work_specials"],))
        assert c.post(f"/trash/{g}/restore", data={"csrf_token": "bad"}, headers=O).status_code == 403
        assert c.post(f"/trash/{g}/restore", data={"csrf_token": tok}, headers={"origin": "http://evil.test"}).status_code == 403
        assert c.get(f"/trash/{g}/restore").status_code == 405                            # 주소를 여는 것만으로는 안 됨
        r = c.post(f"/trash/{g}/restore", data={"csrf_token": tok, "kind": "//evil"}, headers=O, follow_redirects=False)
        assert r.headers["location"].startswith("/trash?msg=")
    with TestClient(app) as c:
        tok = login(c, "tr_hr")
        r = c.post(f"/trash/{s}/restore", data={"csrf_token": tok, "edit": "1"}, headers=O, follow_redirects=False)
        assert r.headers["location"].startswith(f"/hr/specials?row={made['work_specials']}&msg=")      # 복구하고 수정 → 그 건의 수정 창
        assert f'data-row="{made["work_specials"]}"' in c.get("/hr/specials").text
    trash.restore(k, "admin")


def test_delete_buttons_go_to_trash(made):
    """실제 화면의 삭제 주소가 모두 휴지통으로 가고, 되살리면 그대로."""
    e, v, a, m = made["employees"], made["ga_vehicles"], made["ga_assets"], made
    urls = [("rewards", f"/hr/rewards/{m['rewards']}/delete"), ("work_specials", f"/hr/specials/{m['work_specials']}/delete"), ("salaries", f"/hr/salaries/{m['salaries']}/delete"),
            ("appointments", f"/hr/appointments/{m['appointments']}/remove"), ("hr_documents", f"/hr/documents/{m['hr_documents']}/delete"), ("doc_templates", f"/hr/templates/{m['doc_templates']}/delete"),
            ("health_checks", f"/hr/health/{m['health_checks']}/delete"), ("dual_enrollments", f"/hr/dual/{m['dual_enrollments']}/delete"),
            ("employee_insurances", f"/hr/employees/{e}/insurances/{m['employee_insurances']}/delete"), ("offboardings", f"/hr/offboard/{e}/delete"), ("onboardings", f"/hr/onboard/{e}/delete"),
            ("incentive_payments", f"/mgmt/incentive/pay/{m['incentive_payments']}/delete"), ("training_records", f"/mgmt/training/records/{m['training_records']}/delete"),
            ("compliance_tasks", f"/mgmt/compliance/tasks/{m['compliance_tasks']}/delete"), ("ga_contracts", f"/ga/contracts/{m['ga_contracts']}/delete"),
            ("ga_company_items", f"/ga/company/{m['ga_company_items']}/delete"), ("ga_docs", f"/ga/docs/{m['ga_docs']}/delete"), ("ga_seal_logs", f"/ga/seals/{m['ga_seal_logs']}/delete"),
            ("ga_events", f"/ga/events/{m['ga_events']}/delete"), ("ga_event_rules", f"/ga/rules/{m['ga_event_rules']}/delete"), ("ga_vehicle_logs", f"/ga/vlogs/{m['ga_vehicle_logs']}/delete"),
            ("ga_vehicle_maint", f"/ga/maint/{m['ga_vehicle_maint']}/delete"), ("ga_vehicle_fines", f"/ga/fines/{m['ga_vehicle_fines']}/delete"), ("ga_vehicle_damages", f"/ga/damages/{m['ga_vehicle_damages']}/delete"),
            ("ga_vehicle_inspections", f"/ga/vehicles/{v}/inspections/{m['ga_vehicle_inspections']}/delete"), ("ga_vehicles", f"/ga/vehicles/{v}/delete"), ("ga_assets", f"/ga/assets/{a}/delete"),
            ("codes", f"/settings/codes/{m['codes']}/delete"), ("employees", f"/hr/employees/{e}/delete")]
    with TestClient(app) as c:
        tok = login(c, "tr_admin")
        for table, url in urls:
            before, n0 = dump(), query_one("SELECT COALESCE(MAX(id),0) m FROM trash_items")["m"]
            if table == "salaries":                                    # 남은 연중 변경이 그 해 연봉으로 올라가는 동작은 따로
                execute("DELETE FROM salary_steps WHERE id=?", (m["salary_steps"],))
                before = dump()
            r = c.post(url, data={"csrf_token": tok}, headers=O, follow_redirects=False)
            assert r.status_code == 303, (table, url, r.status_code)
            row = query_one("SELECT * FROM trash_items WHERE id>? AND table_name=?", (n0, table))
            assert row and row["root_id"] == m[table] and row["deleted_by"] == "tr_admin", (table, url)
            r = c.post(f"/trash/{row['id']}/restore", data={"csrf_token": tok}, headers=O, follow_redirects=False)
            assert r.status_code == 303 and diff(before, dump()) == {}, (table, diff(before, dump()))
        # 여러 발령을 한 번에
        ids = [execute("INSERT INTO appointments(employee_id, appt_date, appt_type) VALUES (?,?,'전보')", (e, d(-3))) for _ in range(3)]
        c.post("/hr/appointments/remove-selected", data={"csrf_token": tok, "ids": ids}, headers=O)
        assert len(query("SELECT id FROM trash_items WHERE table_name='appointments' AND root_id IN (?,?,?)", ids)) == 3
        page = c.get("/trash?kind=appointments&q=휴지통직원").text
        assert page.count("/restore") == 3
        assert c.get("/trash?q=" + "없는말").text.count("/restore") == 0


def test_purge_only_admin_and_nas_untouched(made, tmp_path):
    root = archive.root()
    os.makedirs(os.path.join(root, "시험"), exist_ok=True)
    f = os.path.join(root, "시험", "서류.pdf")
    with open(f, "wb") as fp:
        fp.write(b"%PDF-1.4")

    def listing():
        return sorted((p, os.path.getsize(os.path.join(p, n)), n) for p, _dd, ns in os.walk(root) for n in ns)
    from app.modules.ga import photos
    os.makedirs(photos._folder(), exist_ok=True)
    a = execute("INSERT INTO ga_assets(asset_no, category, name, photo) VALUES ('TR-P1','PC','사진자산','1')")
    with open(photos.path(a), "wb") as fp:
        fp.write(b"x")
    files = listing()
    tid = rm("ga_assets", a)
    assert os.path.exists(photos.path(a))                                       # 휴지통에 있는 동안은 사진도 그대로
    with TestClient(app) as c:
        tok = login(c, "tr_admin")
        assert "완전히 지울까요" in c.get("/trash").text
        c.post(f"/trash/{tid}/purge", data={"csrf_token": tok}, headers=O)
    assert not query_one("SELECT 1 FROM trash_items WHERE id=?", (tid,)) and not os.path.exists(photos.path(a))
    assert listing() == files                                                    # 보관함(NAS)은 그대로
    assert query_one("SELECT 1 FROM audit_logs WHERE action='휴지통 완전삭제' AND target LIKE '자산%'")


def test_auto_purge_rules(made):
    c, s = rm("ga_docs", made["ga_docs"]), rm("work_specials", made["work_specials"])
    old = (datetime.now() - timedelta(days=61)).strftime("%Y-%m-%d %H:%M:%S")
    trash.auto_purge()
    execute("UPDATE trash_items SET deleted_at=? WHERE id IN (?,?)", (old, c, s))
    assert trash.auto_purge() == 0                                               # 날짜만 지났고 실제로 지켜본 날이 적으면(시계 오류 등) 지우지 않음
    assert query_one("SELECT seen_days n FROM trash_items WHERE id=?", (c,))["n"] == 1          # 하루에 한 번만 셈
    execute("UPDATE trash_items SET deleted_at=?, seen_days=50 WHERE id IN (?,?)", ((datetime.now() - timedelta(days=55)).strftime("%Y-%m-%d %H:%M:%S"), c, s))
    assert trash.auto_purge() == 0 and trash.soon("admin") >= 1 and trash.soon("viewer") == 0
    with TestClient(app) as cl:
        login(cl, "tr_admin")
        assert "7일 안에 완전히 지워집니다" in cl.get("/").text
        assert cl.get("/trash?soon=1").text.count("/restore") == trash.soon("admin")
    execute("UPDATE trash_items SET deleted_at=? WHERE id IN (?,?)", (old, c, s))
    assert trash.auto_purge() == 1
    assert not query_one("SELECT 1 FROM trash_items WHERE id=?", (c,))
    assert query_one("SELECT 1 FROM trash_items WHERE id=?", (s,))               # 법정 보존 자료는 자동으로 지우지 않음
    assert {t for t, m in trash.META.items() if m[3]} == {"employees", "hr_documents", "appointments", "salaries", "salary_steps", "rewards", "work_specials", "health_checks", "training_records"}
    trash.restore(s, "hr")


def test_undo_like_buttons_do_not_fill_trash(made):
    """체크 해제·연중 연봉 자동 정리처럼 프로그램이 정리하는 것은 휴지통에 넣지 않음."""
    from app.modules.hr import repo
    n = query_one("SELECT COUNT(*) c FROM trash_items")["c"]
    sid = repo.add_salary_step(made["employees"], "2023-01-01", 1000)
    repo.promote_salary_steps(made["employees"])
    assert not query_one("SELECT 1 FROM salary_steps WHERE id=?", (sid,))
    assert query_one("SELECT COUNT(*) c FROM trash_items")["c"] == n
