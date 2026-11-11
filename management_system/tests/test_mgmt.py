"""경영관리: fund 손익 읽기(인증서 고정), 인센티브 예산, 인건비 총괄, 인사평가·연봉조정, 교육 이수, 법정의무 일정, 회사 관리 만기."""
import datetime as dt
import hashlib
import http.server
import json
import os
import re
import ssl
import tempfile
import threading

import pytest
from fastapi.testclient import TestClient

from app.db import execute, get_settings, query, query_one, set_setting
from app.main import app
from app.modules.ga import company
from app.modules.hr import repo as hr
from app.modules.mgmt import compliance, fin, incentive, labor, review, training
from app.security import auth
from app.security.crypto import enc

O = {"origin": "http://testserver"}


def _raw(sql):
    import sqlite3
    con = sqlite3.connect(os.environ["BMS_DB_PATH"])
    try:
        return str(con.execute(sql).fetchone()[0])
    finally:
        con.close()


def _client(username, role):
    c = TestClient(app)
    if not auth.query_one("SELECT 1 FROM users WHERE username=?", (username,)):
        auth.create_user(username, username, role, "Mg-Strong-Pass-1!", must_change=False)
    c.post("/login", data={"username": username, "password": "Mg-Strong-Pass-1!"}, headers=O)
    c.headers.update(O)
    c.headers["x-csrf-token"] = re.search(r'name="csrf-token" content="([^"]+)"', c.get("/account").text).group(1)
    return c


@pytest.fixture(scope="module")
def env():
    with TestClient(app):
        a = hr.create_employee({"name": "평가김", "hire_date": "2020-03-01", "birth_date": "1990-01-01", "department": "평가1팀",
                                "emp_no": "M001", "rank": "과장"})
        b = hr.create_employee({"name": "평가이", "hire_date": "2025-07-01", "birth_date": "1992-02-02", "department": "평가1팀",
                                "emp_no": "M002", "rank": "대리"})
        c = hr.create_employee({"name": "평가박", "hire_date": "2019-01-01", "birth_date": "1985-05-05", "department": "평가2팀",
                                "emp_no": "M003", "rank": "부장"})
        hr.upsert_salary(a, 2025, 50_000_000)
        hr.upsert_salary(b, 2025, 36_000_000)
        hr.upsert_salary(c, 2025, 80_000_000)
        yield {"admin": _client("mg_admin", "admin"), "ga": _client("mg_ga", "ga"), "viewer": _client("mg_view", "viewer"),
               "a": a, "b": b, "c": c}


# ── fund 연결: 인증서 지문이 맞을 때만, 열쇠를 실어 한 해만 ───────────────────────
def _self_signed(tmp):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fund-test")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=2)).sign(key, hashes.SHA256()))
    cp, kp = os.path.join(tmp, "c.pem"), os.path.join(tmp, "k.pem")
    open(cp, "wb").write(cert.public_bytes(serialization.Encoding.PEM))
    open(kp, "wb").write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return cp, kp, hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest().upper()


@pytest.fixture(scope="module")
def fake_fund():
    seen = []

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization")))
            if self.headers.get("Authorization") != "Bearer good-key":
                body, code = {"ok": False, "message": "forbidden"}, 403
            elif self.path.endswith("year=2025"):
                body, code = {"ok": True, "year": 2025, "basis": "확정", "period": "연간 실적", "as_of": "2026-11-10 16:56",
                              "items": {"매출액": {"확정": 10_000_000_000}, "영업손익": {"확정": 800_000_000},
                                        "계좌잔액": {"확정": 1}}}, 200
            elif self.path.endswith("year=2024"):
                body, code = {"ok": True, "year": 2024, "basis": "확정", "period": "연간 실적", "as_of": "x",
                              "items": {"매출액": {"확정": 9_000_000_000}, "영업손익": {"확정": 600_000_000}}}, 200
            else:
                body, code = {"ok": False, "message": "없음"}, 404
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    tmp = tempfile.mkdtemp()
    cp, kp, fp = _self_signed(tmp)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cp, kp)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"url": f"https://127.0.0.1:{srv.server_address[1]}", "fp": fp, "seen": seen}
    srv.shutdown()


def test_fund_pull_pins_certificate_and_sends_key(env, fake_fund):
    set_setting("fund_token", "")
    with pytest.raises(fin.FundError, match="설정"):
        fin.fetch_fund(2025)
    set_setting("fund_url", fake_fund["url"])
    set_setting("fund_token", enc("good-key"))
    set_setting("fund_cert_sha256", "AB" * 32)                      # 다른 인증서 → 거절
    with pytest.raises(fin.FundError, match="지문"):
        fin.fetch_fund(2025)
    assert not fake_fund["seen"], "지문이 다르면 열쇠를 보내기 전에 끊어야 함"
    set_setting("fund_cert_sha256", ":".join(fake_fund["fp"][i:i + 2] for i in range(0, 64, 2)))
    d = fin.pull(2025, "tester")
    assert d["items"]["매출액"]["확정"] == 10_000_000_000
    assert "계좌잔액" not in d["items"], "손익 요약 항목만 받아야 함"
    assert fake_fund["seen"][-1] == ("/api/bms/annual?year=2025", "Bearer good-key")
    raw = _raw("SELECT data FROM mgmt_fin_year WHERE year=2025")
    assert raw.startswith("enc:v1:") and "10000000000" not in raw    # 저장도 암호화
    fin.pull(2024)
    s = fin.summary(2025)
    assert s["by"]["매출액"]["cur"] == 10_000_000_000 and round(s["by"]["매출액"]["rate"], 2) == 11.11
    with pytest.raises(fin.FundError, match="없"):
        fin.fetch_fund(2019)
    set_setting("fund_token", enc("bad-key"))
    with pytest.raises(fin.FundError, match="거절"):
        fin.fetch_fund(2025)
    set_setting("fund_token", enc("good-key"))


def test_settings_fund_saves_token_encrypted(env):
    r = env["admin"].post("/settings/fund", data={"fund_url": "https://fund.example.test:3103", "fund_cert_sha256": "CD" * 32,
                                                  "fund_token": "fundbms_secret"}, follow_redirects=False)
    assert r.status_code == 303
    s = get_settings()
    raw = _raw("SELECT value FROM settings WHERE key='fund_token'")
    assert raw.startswith("enc:v1:") and "fundbms_secret" not in raw
    assert "fundbms_secret" not in env["admin"].get("/settings").text
    assert env["admin"].post("/settings/fund", data={"fund_url": "http://x"}, follow_redirects=False).status_code == 303
    assert get_settings()["fund_url"] == "https://fund.example.test:3103"


# ── 인센티브 ────────────────────────────────────────────────────────────
def test_incentive_budget_and_payments(env):
    incentive.save_plan(2026, 120_000_000, "26년", {"평가1팀": 50_000_000, "평가2팀": 10_000_000})
    n = incentive.add_payments(2026, [
        {"pay_date": "2026-06-30", "employee_id": env["a"], "amount": "30,000,000", "reason": "상반기"},
        {"pay_date": "2026-06-30", "department": "평가1팀", "employee_id": "", "amount": "5000000"},
        {"pay_date": "2026-07-15", "employee_id": env["c"], "amount": "9,000,000"},
        {"pay_date": "2026-07-15", "employee_id": env["b"], "amount": ""},      # 빈 금액은 건너뜀
    ])
    assert n == 3
    ov = incentive.overview(2026)
    assert (ov["total"], ov["assigned"], ov["paid"], ov["remain"]) == (120_000_000, 60_000_000, 44_000_000, 76_000_000)
    by = {r["department"]: r for r in ov["rows"]}
    assert by["평가1팀"]["paid"] == 35_000_000 and by["평가1팀"]["remain"] == 15_000_000
    assert by["평가2팀"]["level"] == "warning"                         # 90% 소진
    assert ov["cumulative"][5] == 35_000_000 and ov["cumulative"][6] == 44_000_000
    assert incentive.employee_totals(2026)[env["a"]] == 30_000_000
    with pytest.raises(incentive.IncentiveError, match="2026년"):
        incentive.add_payments(2026, [{"pay_date": "2025-12-31", "employee_id": env["a"], "amount": "1"}])
    assert _raw("SELECT amount FROM incentive_payments LIMIT 1").startswith("enc:v1:")    # 개인 지급액 암호화


def test_incentive_pages(env):
    adm = env["admin"]
    r = adm.post("/mgmt/incentive/pay", data={"year": "2026", "common_date": "2026-08-01", "common_reason": "특별",
                                              "pay_date": ["", ""], "reason": ["", ""], "department": ["평가2팀", "평가2팀"],
                                              "employee_id": ["", str(env["c"])], "amount": ["", "1,000,000"], "note": ["", "메모"]},
                 follow_redirects=False)
    assert r.status_code == 303 and "1" in r.headers["location"]
    p = adm.get("/mgmt/incentive?year=2026")
    assert p.status_code == 200 and "평가2팀" in p.text and "지급 기록" in p.text
    assert "평가박" in adm.get("/mgmt/incentive/export.csv?year=2026").text


# ── 인건비 총괄 ─────────────────────────────────────────────────────────
def test_labor_overview(env):
    set_setting("labor_employer_rate", "10")
    set_setting("labor_retire_rate", "8")
    ov = labor.overview(2025)
    p = {x["name"]: x for x in ov["people"]}
    assert p["평가김"]["salary"] == 50_000_000 and p["평가김"]["months"] == 12
    assert p["평가이"]["months"] == 6 and p["평가이"]["salary"] == 18_000_000       # 7월 입사 → 6개월분
    assert p["평가박"]["employer"] == 8_000_000 and p["평가박"]["retire"] == 6_400_000
    assert ov["labor_ratio"] is not None                               # 2025 매출(fund) 대비
    ov26 = labor.overview(2026)
    q = {x["name"]: x for x in ov26["people"]}
    assert q["평가김"]["annual"] == 50_000_000 and q["평가김"]["base_year"] == 2025   # 2026 연봉 없음 → 직전 연봉
    assert q["평가김"]["incentive"] == 30_000_000
    dept = {r["department"]: r for r in ov26["rows"]}
    assert dept["평가1팀"]["incentive"] == 35_000_000                    # 부서 지급분 포함
    assert env["admin"].get("/mgmt/labor?year=2025").status_code == 200


# ── 인사평가·연봉조정 ───────────────────────────────────────────────────
def test_review_flow(env):
    adm = env["admin"]
    assert adm.post("/mgmt/review/create", data={"year": "2025"}, follow_redirects=False).status_code == 303
    c = review.get(2025)
    assert c["apply_year"] == 2026 and c["eval_start"] == "2026-05-01" and c["status"] == "준비"
    r = adm.post("/mgmt/review/2025/setup", data={"scenarios": "5, 6, 7", "grades": "S:+2, A:+1, B:0, C:-1, D:-2",
                                                  "base_rate": "", "round_unit": "10000", "eval_start": "2026-05-01",
                                                  "eval_end": "2026-05-20", "effective_date": "2026-01-01", "min_wage_next": ""},
                 follow_redirects=False)
    assert r.status_code == 303
    sc = review.scenarios(review.get(2025))
    assert [x["rate"] for x in sc["rows"]] == [5.0, 6.0, 7.0]
    six = sc["rows"][1]
    fixed = {x["employee"]["name"]: x for x in review.compute(review.get(2025), base_rate=6, use_overrides=False, with_grades=False)["rows"]}
    assert (fixed["평가김"]["raise"], fixed["평가이"]["raise"], fixed["평가박"]["raise"]) == (3_000_000, 2_160_000, 4_800_000)
    assert six["increase_fixed"] == sum(x["raise"] or 0 for x in fixed.values())
    assert six["op_share"] is not None and six["op_growth_share"] is not None     # fund 2025·2024 영업이익
    # '이 안으로 정하기' — 다른 칸은 그대로
    adm.post("/mgmt/review/2025/setup", data={"base_rate": "6"})
    c = review.get(2025)
    assert c["base_rate"] == 6.0 and c["eval_end"] == "2026-05-20" and c["grades"][0] == ["S", 2.0]
    # 평가 입력 (화면 폼)
    r = adm.post("/mgmt/review/2025/items", data={"employee_id": [str(env["a"]), str(env["b"]), str(env["c"])],
                                                   "grade": ["S", "B", "C"], "add_rate": ["", "", "-0.5"],
                                                   "final_rate": ["", "8", ""], "comment": ["우수", "", ""],
                                                   "evaluator": ["팀장", "", ""]}, follow_redirects=False)
    assert r.status_code == 303
    c = review.get(2025)
    assert c["status"] == "평가중"
    res = review.compute(c)
    rows = {x["employee"]["name"]: x for x in res["rows"]}
    assert rows["평가김"]["final_rate"] == 8.0 and rows["평가김"]["new_salary"] == 54_000_000
    assert rows["평가이"]["final_rate"] == 8.0 and rows["평가이"]["new_salary"] == 38_880_000   # 개별 조정 우선
    assert rows["평가박"]["final_rate"] == 5.5 and rows["평가박"]["new_salary"] == 84_400_000   # 가산 직접(-0.5)
    assert query_one("SELECT comment FROM review_items WHERE employee_id=?", (env["a"],))["comment"] == "우수"
    assert _raw(f"SELECT comment FROM review_items WHERE employee_id={env['a']}").startswith("enc:v1:")
    # 부서장 평가표: 연봉 금액 없음, 올리기
    sheet = adm.get("/mgmt/review/2025/sheet.csv?department=평가1팀").text
    assert "평가김" in sheet and "평가박" not in sheet and "50000000" not in sheet and "연봉" not in sheet
    up = "﻿부서,사번,성명,직급,입사일,등급,의견\n평가1팀,M001,평가김,과장,2020-03-01,A,수정\n평가2팀,,평가박,부장,,B,\n"
    r = adm.post("/mgmt/review/2025/sheet", files={"file": ("s.csv", up.encode("utf-8"), "text/csv")}, follow_redirects=False)
    assert r.status_code == 303 and "2" in r.headers["location"]
    assert review.items(review.get(2025))[env["a"]]["grade"] == "A"
    with pytest.raises(review.ReviewError, match="없는 등급"):
        review.save_items(review.get(2025), [{"employee_id": env["a"], "grade": "Z"}])
    # 확정 → 잠금 → 반영
    assert adm.post("/mgmt/review/2025/status", data={"status": "확정"}, follow_redirects=False).status_code == 303
    c = review.get(2025)
    assert c["status"] == "확정" and c["locked"]
    with pytest.raises(review.ReviewError, match="확정"):
        review.save_items(c, [{"employee_id": env["a"], "grade": "S"}])
    assert adm.post("/mgmt/review/2025/apply", follow_redirects=False).status_code == 303
    assert review.get(2025)["status"] == "반영완료"
    sal = {s["employee_id"]: s for s in hr.all_salaries() if s["year"] == 2026}
    assert sal[env["a"]]["annual_salary"] == 53_500_000                # 6% + A(+1)
    assert sal[env["b"]]["annual_salary"] == 38_880_000
    assert sal[env["c"]]["annual_salary"] == 84_400_000                 # 6% + 직접 가산 -0.5 (등급 B로 바뀌어도 직접 넣은 가산 유지)
    assert "인사평가" in (sal[env["a"]]["note"] or "")
    p = adm.get("/mgmt/review?year=2025")
    assert p.status_code == 200 and "반영완료" in p.text
    assert "53500000" in adm.get("/mgmt/review/2025/result.csv").text


# ── 교육 이수 ───────────────────────────────────────────────────────────
def test_training_matrix(env, monkeypatch):
    monkeypatch.setenv("BMS_TODAY", "2026-08-01")
    cs = {c["name"]: c for c in training.courses()}
    assert "직장 내 성희롱 예방교육" in cs and "고소작업대(차) 특별교육" in cs
    sh, safe, lift = cs["직장 내 성희롱 예방교육"], cs["산업안전보건 정기교육"], cs["고소작업대(차) 특별교육"]
    training.add_records(safe["id"], [env["a"], env["b"]], "2026-03-10", 6, "집합", "", "")
    training.add_records(safe["id"], [env["c"]], "2026-04-10", 3, "온라인", "", "")
    training.add_records(sh["id"], [env["a"]], "2026-02-01", 1, "온라인", "", "")
    training.set_targets(lift["id"], [env["c"]])
    m = training.matrix(2026)
    ci = {c["id"]: i for i, c in enumerate(m["courses"])}
    row = {r["employee"]["name"]: r for r in m["rows"]}
    assert row["평가김"]["cells"][ci[safe["id"]]]["status"] == "done"          # 상반기 6h ✓, 하반기는 아직
    assert row["평가박"]["cells"][ci[safe["id"]]]["status"] == "partial"       # 3h < 6h
    assert row["평가김"]["cells"][ci[sh["id"]]]["status"] == "done"
    assert row["평가이"]["cells"][ci[sh["id"]]]["status"] == "upcoming"        # 연말까지 남음
    assert row["평가김"]["cells"][ci[lift["id"]]] is None                      # 지정 대상 아님
    assert row["평가박"]["cells"][ci[lift["id"]]]["status"] == "upcoming"
    adm = env["admin"]
    assert adm.get("/mgmt/training?year=2026").status_code == 200
    assert adm.get("/mgmt/training?year=2026&tab=records").status_code == 200
    assert adm.get("/mgmt/training?tab=courses").status_code == 200
    r = adm.post("/mgmt/training/records", data={"year": "2026", "course_id": str(lift["id"]), "edu_date": "2026-07-01",
                                                 "hours": "16", "method": "외부 위탁", "emp_ids": [str(env["c"])]}, follow_redirects=False)
    assert r.status_code == 303
    m = training.matrix(2026)
    row = {r["employee"]["name"]: r for r in m["rows"]}
    assert row["평가박"]["cells"][ci[lift["id"]]]["status"] == "done"
    assert "미이수" in adm.get("/mgmt/training/export.csv?year=2026").text or "예정" in adm.get("/mgmt/training/export.csv?year=2026").text


# ── 법정의무 일정 + 회사 관리 만기 ───────────────────────────────────────
def test_compliance_and_company(env, monkeypatch):
    monkeypatch.setenv("BMS_TODAY", "2026-08-20")
    set_setting("compliance_since", "2026-04-01")
    tid = compliance.save_task({"title": "시험 신고", "category": "세무", "cycle": "분기", "day": "", "is_active": "1"})
    t = query_one("SELECT * FROM compliance_tasks WHERE id=?", (tid,))
    occ = compliance.occurrences(t, 2026)
    assert [o["due"].isoformat() for o in occ] == ["2026-03-31", "2026-06-30", "2026-09-30", "2026-12-31"]
    compliance.mark_done(tid, "2026-Q1", "2026-03-30", "tester")
    iid = company.save({"category": "인증·확인서", "name": "중소기업 확인서", "expire_date": "2026-09-10", "cycle": "매년",
                        "alert_days": "60", "status": "유효"})
    execute("INSERT INTO ga_contracts(category, title, counterparty, end_date, status) VALUES ('임대차','본사 임대','건물주','2026-12-31','유효')")
    ev = compliance.calendar_year(2026)
    mine = {e["title"]: e for e in ev if e["source"] == "의무" and e["title"].startswith("시험 신고")}
    assert mine["시험 신고 (1분기)"]["status"] == "done" and mine["시험 신고 (2분기)"]["status"] == "overdue"
    assert mine["시험 신고 (3분기)"]["status"] == "upcoming"
    mk = query_one("SELECT id FROM compliance_tasks WHERE title='원천세 신고·납부'")["id"]
    st = {e["period"]: e["status"] for e in ev if e.get("task_id") == mk}
    assert st["2026-02"] == "before" and st["2026-05"] == "overdue"     # 관리 시작 전은 밀린 일로 세지 않음
    assert any(e["source"] == "회사관리" and "중소기업 확인서" in e["title"] for e in ev)
    assert any(e["source"] == "계약" and "본사 임대" in e["title"] for e in ev)
    assert any(e["source"] == "교육" for e in ev)
    assert any(e["title"].startswith("원천세") for e in ev)            # 기본 일정
    # 회사 관리 만기 알림 → 총무 대시보드
    al = company.alerts()
    assert al and al[0]["category"] == "회사관리" and "D-21" in al[0]["message"]
    company.renew(iid, "", "2026-08-20", "재발급", "tester")
    assert company.get(iid)["expire_date"] == "2027-09-10"
    with pytest.raises(company.CompanyError):
        company.renew(iid, "2026-01-01", "", "", "")
    assert company.history(iid)[0]["old_expire"] == "2026-09-10"
    adm, ga, viewer = env["admin"], env["ga"], env["viewer"]
    assert adm.get("/mgmt/compliance?year=2026").status_code == 200
    assert adm.get("/mgmt/compliance?year=2026&show=all&src=의무").status_code == 200
    r = ga.post("/mgmt/compliance/done", data={"year": "2026", "task_id": str(tid), "period": "2026-Q2", "done_date": "2026-08-20"},
                follow_redirects=False)
    assert r.status_code == 303
    assert ga.get("/ga/company").status_code == 200 and "중소기업 확인서" in ga.get("/ga/company").text
    assert ga.post("/ga/company/save", data={"category": "보험·보증", "name": "화재보험", "expire_date": "2026-10-01"},
                   follow_redirects=False).status_code == 303
    assert viewer.get("/ga/company").status_code == 200
    assert viewer.post("/ga/company/save", data={"category": "기타", "name": "x"}, follow_redirects=False).status_code == 403
    assert "회사관리" in str(__import__("app.modules.ga.services", fromlist=["x"]).build_alerts([], [], []))


# ── 권한 ────────────────────────────────────────────────────────────────
def test_mgmt_permissions(env):
    ga, viewer, adm = env["ga"], env["viewer"], env["admin"]
    for path in ("/mgmt/labor", "/mgmt/review", "/mgmt/incentive", "/mgmt/labor/export.csv?year=2026"):
        assert ga.get(path, follow_redirects=False).status_code == 403, path
        assert viewer.get(path, follow_redirects=False).status_code == 403, path
        assert adm.get(path).status_code == 200, path
    assert ga.get("/mgmt/training").status_code == 200 and ga.get("/mgmt/compliance").status_code == 200
    assert viewer.get("/mgmt/training", follow_redirects=False).status_code == 403
    assert ga.post("/mgmt/fin/pull", data={"year": "2025"}, follow_redirects=False).status_code == 403
    assert ga.post("/settings/fund", data={"fund_token": "x"}, follow_redirects=False).status_code == 403
    menu = adm.get("/hr/dashboard").text
    assert "인건비 총괄" in menu and "회사 관리 만기" in menu
    gmenu = ga.get("/ga/dashboard").text
    assert "교육 이수" in gmenu and "인건비 총괄" not in gmenu
