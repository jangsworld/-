"""입사 절차 안내, 건강검진, 인사기록카드 인쇄, 인사평가 일괄 적용, 증명서 대장 맞춤, fund 인증서 이름 확인."""
import re
import ssl

import pytest
from fastapi.testclient import TestClient

from app import mailer
from app.db import execute, query, query_one, set_setting
from app.main import app
from app.modules.hr import health, offboard, onboard, repo, services
from app.modules.mgmt import fin, review
from app.security import auth
from app.security.crypto import enc

ORIGIN = {"origin": "http://testserver"}


def _client(user, role):
    c = TestClient(app)
    if not auth.query_one("SELECT 1 FROM users WHERE username=?", (user,)):
        auth.create_user(user, user, role, "Strong-Pass-123!", must_change=False)
    c.post("/login", data={"username": user, "password": "Strong-Pass-123!"}, headers=ORIGIN)
    c.headers.update(ORIGIN)
    c.headers["x-csrf-token"] = re.search(r'name="csrf-token" content="([^"]+)"', c.get("/account").text).group(1)
    return c


@pytest.fixture(scope="module")
def clients():
    with TestClient(app):
        yield {r: _client("hx_" + r, r) for r in ("admin", "hr", "viewer")}


def _emp():
    return next(e for e in repo.list_employees(status="재직(전체)") if e.get("email") and e.get("department"))


# ── 입사 절차 안내: 퇴사 절차와 같은 틀, 자료는 따로 ─────────────────────────
def test_onboard_is_separate_from_offboard(clients, monkeypatch):
    hr, e = clients["hr"], _emp()
    sent = []
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(mailer, "send", lambda to, subject, body, *a, **k: sent.append((to, subject, body)))
    assert onboard.T_MAIN == "onboardings" and offboard.T_MAIN == "offboardings" and onboard.NOTICE_KEY == "a1"
    page = hr.get("/hr/onboard").text
    assert "입사 절차 안내" in page and "입사 처리 단계" in page and "{입사일}" in page and "퇴사 처리 단계" not in page and "퇴직 예정일" not in page
    page = hr.get(f"/hr/onboard/{e['id']}").text
    assert f'value="{e["hire_date"]}"' in page and "서류 제출 기한" in page          # 인사기록카드 입사일이 기본값
    # 서류 제출 기한은 입사일보다 늦어도 됨 (퇴사의 인수인계 기한과 다름)
    hr.post(f"/hr/onboard/{e['id']}/save", data={"last_day": "2027-02-01", "handover_due": "2027-02-05", "successor": "김담당 대리"})
    ob = onboard.get(e["id"])
    assert ob["last_day"] == "2027-02-01" and ob["handover_due"] == "2027-02-05" and offboard.get(e["id"]) is None
    assert repo.get_employee(e["id"])["hire_date"] == e["hire_date"]                  # 인사기록카드는 그대로
    page = hr.get(f"/hr/onboard/{e['id']}").text
    assert "2027년 2월 1일" in page and "김담당 대리" in page and "2027-02-15" in page   # 건강보험 신고 기한(14일)
    hr.post(f"/hr/onboard/{e['id']}/send", data={"target": "employee", "subject": "입사 안내", "body": "<p>환영합니다</p>"})
    assert sent and sent[-1][0] == e["email"] and "a1" in onboard.checks(e["id"]) and not offboard.checks(e["id"])
    assert query_one("SELECT COUNT(*) c FROM onboard_mails WHERE employee_id=?", (e["id"],))["c"] == 1
    assert query_one("SELECT COUNT(*) c FROM offboard_mails WHERE employee_id=?", (e["id"],))["c"] == 0
    # 단계 수정도 따로
    base = [{"k": sk, "name": n, "when": w, "items": [{"k": k, "text": t, "hint": h} for k, t, h in items]} for sk, n, w, items in onboard.STAGES]
    base[0]["name"] = "입사 확정(고침)"
    assert hr.post("/hr/onboard/stages", json={"stages": base}).status_code == 200
    assert onboard.stages()[0][1] == "입사 확정(고침)" and onboard.stages_custom() and not offboard.stages_custom()
    hr.post("/hr/onboard/stages", json={"reset": True})
    assert "/hr/onboard" in hr.get("/hr/dashboard").text
    assert clients["viewer"].get("/hr/onboard", follow_redirects=False).status_code == 403
    hr.post(f"/hr/onboard/{e['id']}/delete")
    assert onboard.get(e["id"]) is None


# ── 건강검진 ────────────────────────────────────────────────────────────
def test_health_overview_cycle_and_roles(clients, monkeypatch):
    monkeypatch.setenv("BMS_TODAY", "2026-11-21")
    hr = clients["hr"]
    emps = [e for e in repo.list_employees(status="재직(전체)") if e["hire_date"] < "2025-01-01"][:3]
    a, b, c = emps
    try:
        health.set_default_cycle("2")
        st = lambda e: next(r for r in health.overview(2026)["rows"] if r["emp"]["id"] == e["id"])["status"]   # noqa: E731
        assert st(a) == st(b) == "todo"
        r = hr.post("/hr/health/save", data={"employee_ids": [a["id"], b["id"]], "check_date": "2026-06-10", "kind": "일반", "institution": "가나병원", "note": "재검 예정"}, follow_redirects=False)
        assert r.status_code == 303 and st(a) == st(b) == "done"
        import sqlite3
        from app.config import settings
        assert sqlite3.connect(settings.db_path).execute("SELECT note FROM health_checks WHERE employee_id=?", (a["id"],)).fetchone()[0].startswith("enc:v1:")
        hr.post("/hr/health/save", data={"employee_ids": [a["id"]], "check_date": "2026-06-10", "kind": "일반"})     # 같은 날 같은 검진은 한 번만
        assert query_one("SELECT COUNT(*) n FROM health_checks WHERE employee_id=?", (a["id"],))["n"] == 1
        # 2년 주기: 지난해에 받았으면 올해는 대상 아님. 매년 주기로 바꾸면 대상
        hr.post("/hr/health/save", data={"employee_ids": [c["id"]], "check_date": "2025-09-01", "kind": "일반"})
        assert st(c) == "skip"
        hr.post("/hr/health/cycle", data={"employee_ids": [c["id"]], "cycle": "1"})
        assert st(c) == "todo"
        hr.post("/hr/health/cycle", data={"employee_ids": [c["id"]], "cycle": ""})
        assert st(c) == "skip"
        # 잘못된 값은 거절
        for bad in ({"check_date": "2027-01-01", "kind": "일반"}, {"check_date": "", "kind": "일반"}, {"check_date": "2026-06-11", "kind": "없는구분"}):
            hr.post("/hr/health/save", data={"employee_ids": [c["id"]], **bad})
        assert query_one("SELECT COUNT(*) n FROM health_checks WHERE employee_id=? AND check_date>='2026-01-01'", (c["id"],))["n"] == 0
        page = hr.get("/hr/health?year=2026").text
        assert "가나병원" in page and "재검 예정" in page and "올해 대상 아님" in page
        assert a["name"] not in hr.get("/hr/health?year=2026&show=todo").text.split("<tbody>")[1].split("</tbody>")[0] or st(a) != "done"
        assert clients["viewer"].get("/hr/health", follow_redirects=False).status_code == 403
        rid = query_one("SELECT id FROM health_checks WHERE employee_id=?", (b["id"],))["id"]
        hr.post(f"/hr/health/{rid}/delete", data={"year": "2026"})
        assert st(b) == "todo"
    finally:
        execute("DELETE FROM health_checks WHERE employee_id IN (?,?,?)", (a["id"], b["id"], c["id"]))
        execute("DELETE FROM health_cycles")


# ── 인사기록카드 인쇄 ───────────────────────────────────────────────────
def test_employee_print_page(clients):
    e = _emp()
    r = clients["hr"].get(f"/hr/employees/{e['id']}/print")
    assert r.status_code == 200 and "인사기록카드" in r.text and e["name"] in r.text and "교육 이수" in r.text
    assert "주민등록번호·연봉·건강 정보는 싣지 않았습니다" in r.text and "annual_salary" not in r.text
    assert clients["viewer"].get(f"/hr/employees/{e['id']}/print", follow_redirects=False).status_code == 403
    assert query("SELECT 1 FROM audit_logs WHERE action='인사기록카드 인쇄 화면'")
    assert f"/hr/employees/{e['id']}/print" in clients["hr"].get(f"/hr/employees/{e['id']}").text


# ── 증명서: 화면에서 고친 용도·발급일이 대장에도 ──────────────────────────
def test_cert_edit_updates_ledger(clients):
    hr, e = clients["hr"], _emp()
    tpl = repo.list_templates("certificate", active_only=True)[0]
    r = hr.post("/hr/certificates/new", data={"employee_id": e["id"], "template_id": tpl["id"], "purpose": "금융기관 제출용"}, follow_redirects=False)
    doc_id = int(r.headers["location"].split("/")[-1].split("?")[0])
    d = repo.get_document(doc_id)
    assert d["purpose"] == "금융기관 제출용"
    content = d["content"].replace("금융기관 제출용", "관공서 <b>제출</b>용").replace(services._kdate(d["signed_date"]), "2026년 12월 01일")
    hr.post(f"/hr/api/documents/{doc_id}", json={"content": content, "signed_date": d["signed_date"]})
    d2 = repo.get_document(doc_id)
    assert d2["purpose"] == "관공서 제출용" and d2["signed_date"] == "2026-12-01"
    assert "관공서 제출용" in hr.get("/hr/certificates").text
    # 옆 칸에서 발급일을 직접 바꾸면 그 값을 따름
    hr.post(f"/hr/api/documents/{doc_id}", json={"content": content, "signed_date": "2026-12-05"})
    assert repo.get_document(doc_id)["signed_date"] == "2026-12-05"
    assert services.cert_fields("<p>표가 없는 글</p>") == {}
    execute("DELETE FROM hr_documents WHERE id=?", (doc_id,))
    repo.release_cert_no(d["doc_no"])


# ── 인사평가: 이름 조회 + 일괄 적용 ──────────────────────────────────────
def test_review_name_filter_and_bulk(clients):
    admin = clients["admin"]
    year = 2031
    c = review.get(year) or review.create(year, "t")
    rows = review.compute(c)["rows"]
    grades = [g for g, _ in c["grades"]]
    assert rows and grades
    from app.db import dept_scope
    a = next(r["employee"] for r in rows if r["employee"].get("department"))
    b = next(r["employee"] for r in rows if (r["employee"].get("department") or "") not in dept_scope(a["department"]) and a["name"] not in r["employee"]["name"])
    review.save_items(c, [{"employee_id": a["id"], "grade": "", "add_rate": "", "final_rate": "", "comment": "메모 유지", "evaluator": ""}])
    page = admin.get(f"/mgmt/review?year={year}&q={a['name']}").text
    assert f'data-emp="{a["id"]}"' in page and f'data-emp="{b["id"]}"' not in page and "일괄 적용" in page
    # 이름으로 조회한 사람에게만
    r = admin.post(f"/mgmt/review/{year}/bulk", data={"q": a["name"], "department": "", "grade": grades[0], "final_rate": "4.5"}, follow_redirects=False)
    assert r.status_code == 303
    it = review.items(review.get(year))
    same_name = [x["employee"]["id"] for x in rows if a["name"] in x["employee"]["name"]]
    assert it[a["id"]]["grade"] == grades[0] and it[a["id"]]["final_rate"] == 4.5 and it[a["id"]]["comment"] == "메모 유지"
    assert set(it) == set(same_name)
    # 부서 전체에 등급만: 최종 직접 %는 그대로
    admin.post(f"/mgmt/review/{year}/bulk", data={"department": a["department"], "q": "", "grade": grades[-1]})
    it = review.items(review.get(year))
    dept_ids = [x["employee"]["id"] for x in admin_rows(year, a["department"])]
    assert all(it[i]["grade"] == grades[-1] for i in dept_ids) and it[a["id"]]["final_rate"] == 4.5 and b["id"] not in it
    # 최종 직접 칸 비우기, 등급은 그대로
    admin.post(f"/mgmt/review/{year}/bulk", data={"department": a["department"], "q": "", "grade": "__keep__", "clear_final": "1"})
    it = review.items(review.get(year))
    assert it[a["id"]]["final_rate"] is None and it[a["id"]]["grade"] == grades[-1]
    # 바꿀 것이 없거나 잘못된 값은 그대로 둠
    admin.post(f"/mgmt/review/{year}/bulk", data={"department": "", "q": "", "grade": "__keep__"})
    admin.post(f"/mgmt/review/{year}/bulk", data={"department": "", "q": "", "grade": "없는등급"})
    admin.post(f"/mgmt/review/{year}/bulk", data={"department": "", "q": "", "grade": "__keep__", "final_rate": "많이"})
    assert b["id"] not in review.items(review.get(year))
    assert clients["viewer"].post(f"/mgmt/review/{year}/bulk", data={"grade": grades[0]}, follow_redirects=False).status_code == 403
    execute("DELETE FROM review_items WHERE cycle_id=?", (c["id"],))
    execute("DELETE FROM review_cycles WHERE id=?", (c["id"],))


def admin_rows(year, department):
    from app.modules.mgmt.routes import _review_rows
    return _review_rows(review.compute(review.get(year))["rows"], department, "")


# ── fund: 인증서 이름으로 확인 ──────────────────────────────────────────
def test_fund_name_mode_rejects_untrusted_cert_before_sending_key(monkeypatch):
    import http.server
    import os
    import tempfile
    import threading

    from tests.test_mgmt import _self_signed
    seen = []

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            seen.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

    cp, kp, _fp = _self_signed(tempfile.mkdtemp())
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cp, kp)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    old = {k: query_one("SELECT value FROM settings WHERE key=?", (k,)) for k in ("fund_url", "fund_token", "fund_cert_sha256", "fund_tls_name")}
    try:
        set_setting("fund_url", f"https://127.0.0.1:{srv.server_address[1]}")
        set_setting("fund_token", enc("good-key"))
        set_setting("fund_cert_sha256", "")
        set_setting("fund_tls_name", "")
        assert not fin.fund_configured()
        with pytest.raises(fin.FundError, match="설정"):
            fin.fetch_fund(2025)
        set_setting("fund_tls_name", "fund.example.com")
        assert fin.fund_configured()
        with pytest.raises(fin.FundError, match="확인하지 못했습니다"):       # 믿을 수 없는(자체 서명) 인증서 → 거절
            fin.fetch_fund(2025)
        assert not seen, "인증서 확인 전에 열쇠를 보내면 안 됨"
        set_setting("fund_tls_name", "https://x/y")
        with pytest.raises(fin.FundError, match="이름"):
            fin.fetch_fund(2025)
        assert fin.NAME_RE.match("fund.example.ts.net") and not fin.NAME_RE.match("a b.com") and not fin.NAME_RE.match("10.0.0.1:3103")
    finally:
        srv.shutdown()
        for k, v in old.items():
            set_setting(k, (v or {}).get("value") or "")


def test_fund_settings_form_saves_name_and_clears_fingerprint(clients):
    admin = clients["admin"]
    old = {k: (query_one("SELECT value FROM settings WHERE key=?", (k,)) or {}).get("value") or "" for k in ("fund_url", "fund_cert_sha256", "fund_tls_name")}
    try:
        set_setting("fund_cert_sha256", "AB" * 32)
        admin.post("/settings/fund", data={"fund_url": "https://10.0.0.5:3103", "fund_tls_name": "fund.example.com", "fund_cert_sha256": "", "fund_token": ""})
        s = {r["key"]: r["value"] for r in query("SELECT key, value FROM settings WHERE key LIKE 'fund_%'")}
        assert s["fund_tls_name"] == "fund.example.com" and s["fund_cert_sha256"] == "" and s["fund_url"] == "https://10.0.0.5:3103"
        admin.post("/settings/fund", data={"fund_url": "", "fund_tls_name": "https://bad name", "fund_cert_sha256": "", "fund_token": ""})
        assert query_one("SELECT value FROM settings WHERE key='fund_tls_name'")["value"] == "fund.example.com"
        assert "fund 인증서 이름" in admin.get("/settings").text
    finally:
        for k, v in old.items():
            set_setting(k, v)
