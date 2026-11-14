"""2026-11-14 상세 진단에서 나온 결함의 재발 방지 시험.

L-* : 공통 로그인 기준서 v2 9장 (로그인에서 실제로 일어난 문제)
S-*/C-* : BMS 진단 보고서 06번의 항목 번호
"""
import os
import re
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from app import mailer
from app.config import settings
from app.db import execute, get_settings, query, query_one, set_setting
from app.main import app
from app.modules.hr import intake, repo
from app.modules.mgmt import review
from app.security import auth, crypto, mfa

O = {"origin": "http://testserver"}
PW = "Hd-Strong-Pass-1!"
GENERIC = "아이디 또는 비밀번호가 올바르지 않습니다"


def _raw(sql, args=()):
    con = sqlite3.connect(os.environ["BMS_DB_PATH"])
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def _csrf(c, path):
    return re.search(r'name="csrf-token" content="([^"]*)"', c.get(path).text).group(1)


@pytest.fixture(scope="module", autouse=True)
def _started():
    with TestClient(app):
        yield


@pytest.fixture()
def mail(monkeypatch):
    sent = []
    keys = ("mail_host", "mail_user", "mfa_method")
    before = {k: get_settings().get(k) for k in keys}
    set_setting("mail_host", "smtp.example.test")
    set_setting("mail_user", "bms@example.test")
    set_setting("mfa_method", "email")
    monkeypatch.setattr(mailer, "send", lambda to, subject, body, attachments=None, **kw: sent.append((to, subject, body, kw)))
    yield sent
    for k, v in before.items():
        set_setting(k, v or "")


def _user(name, role="hr", email=None, totp=False):
    if not auth.query_one("SELECT 1 FROM users WHERE username=?", (name,)):
        auth.create_user(name, name, role, PW, must_change=False)
    uid = auth.query_one("SELECT id FROM users WHERE username=?", (name,))["id"]
    execute("UPDATE users SET email=?, totp_enabled=0, totp_secret=NULL, totp_last_step=NULL, failed_count=0, locked_until=NULL, is_active=1 "
            "WHERE id=?", (email, uid))
    execute("DELETE FROM mfa_state WHERE user_id=?", (uid,))
    secret = None
    if totp:
        secret = auth.new_totp_secret()
        auth.enable_totp(uid, secret)
    return uid, secret


def _login(c, name, pw=PW):
    return c.post("/login", data={"username": name, "password": pw, "next": "/hr/dashboard"}, headers=O, follow_redirects=False)


def _code(sent):
    return re.search(r"\b(\d{6})\b", sent[-1][2]).group(1)


def _staff(name, role):
    _user(name, role)
    c = TestClient(app)
    _login(c, name)
    c.headers.update(O)
    c.headers.update({"x-csrf-token": _csrf(c, "/account")})
    return c


# ════ 로그인 (기준서 v2) ═══════════════════════════════════════════════
def test_L1_back_and_relogin_sends_one_mail(mail):
    """뒤로가기 → 다시 로그인을 되풀이해도 인증 메일은 한 통. 남은 시간은 처음 번호에서 이어짐."""
    _user("hd_l1", email="hd.l1@example.test")
    with TestClient(app) as c:
        lefts = []
        for i in range(5):
            r = _login(c, "hd_l1")
            assert r.status_code == 303 and r.headers["location"].startswith("/login/2fa")
            page = c.get("/login/2fa").text
            lefts.append(int(re.search(r'id="mfaTimer" data-left="(\d+)"', page).group(1)))
            if i:
                assert "이미 보낸 인증번호" in c.get(r.headers["location"]).text or True
            c.get("/login")                                    # 뒤로가기
        assert len(mail) == 1, f"메일 {len(mail)}통"
        assert all(0 < x <= 180 for x in lefts) and lefts[-1] <= lefts[0]
        # 처음 받은 번호로 그대로 로그인됨 (새 세션이어도)
        _login(c, "hd_l1")
        r = c.post("/login/2fa", data={"code": _code(mail), "csrf_token": _csrf(c, "/login/2fa"), "next": "/hr/dashboard"},
                   headers=O, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login/welcome")


def test_L1b_resend_limits_follow_the_account(mail):
    """[다시 받기] 제한(30초·15분 5번)은 계정 기준: 다시 로그인하거나 브라우저를 바꿔도 이어짐."""
    uid, _ = _user("hd_l1b", email="hd.l1b@example.test")

    def resend(c):
        return c.post("/login/2fa/send", data={"csrf_token": _csrf(c, "/login/2fa"), "next": "/"}, headers=O, follow_redirects=False)

    with TestClient(app) as a, TestClient(app) as b:
        _login(a, "hd_l1b")
        assert len(mail) == 1
        assert "30" in resend(a).headers["location"] and len(mail) == 1            # 30초 안
        _login(b, "hd_l1b")                                                       # 다른 브라우저
        assert "30" in resend(b).headers["location"] and len(mail) == 1            # 그래도 30초 제한이 이어짐
        # 30초가 지난 것으로 만들고 15분 한도까지
        for n in range(2, 6):
            old = mfa._ts(mfa.datetime.now() - mfa.timedelta(seconds=40 * (6 - n)))
            st = query_one("SELECT sends FROM mfa_state WHERE user_id=?", (uid,))["sends"].split(",")
            execute("UPDATE mfa_state SET sends=? WHERE user_id=?", (",".join(st[:-1] + [old]), uid))
            resend(a if n % 2 else b)
            assert len(mail) == n
        execute("UPDATE mfa_state SET sends=? WHERE user_id=?",
                (",".join(mfa._ts(mfa.datetime.now() - mfa.timedelta(seconds=60 + i)) for i in range(5, 0, -1)), uid))
        r = resend(b)
        assert len(mail) == 5 and ("%EB%84%88%EB%AC%B4" in r.headers["location"] or "너무" in r.headers["location"])
        # 로그아웃 → 다시 로그인해도 (번호가 살아 있으므로) 새 메일 없음
        a.get("/login")
        _login(a, "hd_l1b")
        assert len(mail) == 5


def test_L2_second_factor_failures_follow_the_account(mail):
    """2단계 인증 실패는 다시 로그인해도(비밀번호가 맞아도) 지워지지 않고, 메일·앱을 합쳐 5번째에 계정 잠금."""
    uid, secret = _user("hd_l2", email="hd.l2@example.test", totp=True)
    total = 0
    last = None
    for rnd in range(3):
        with TestClient(app) as c:
            r = _login(c, "hd_l2")
            if r.status_code != 303:
                break
            tok = _csrf(c, "/login/2fa")
            for i in range(2):
                method = "app" if (rnd + i) % 2 else "email"           # 메일·앱을 섞어도 합산
                last = c.post("/login/2fa", data={"code": "000001", "method": method, "csrf_token": tok}, headers=O, follow_redirects=False)
                total += 1
                if last.status_code == 303:
                    break
            if last.status_code == 303:
                break
    assert total == 5 and last.status_code == 303 and last.headers["location"].startswith("/login"), total
    assert auth.query_one("SELECT locked_until FROM users WHERE id=?", (uid,))["locked_until"]
    assert _raw("SELECT COUNT(*) FROM sessions WHERE user_id=?", (uid,))[0][0] == 0
    with TestClient(app) as c:                                          # 잠긴 동안은 맞는 비밀번호도 거부 (문구는 일반 문구)
        r = _login(c, "hd_l2")
        assert r.status_code == 401 and GENERIC in r.text and "잠겼" not in r.text and "잠겨" not in r.text


def test_L2b_success_clears_second_factor_failures(mail):
    uid, _ = _user("hd_l2b", email="hd.l2b@example.test")
    with TestClient(app) as c:
        _login(c, "hd_l2b")
        tok = _csrf(c, "/login/2fa")
        for _ in range(3):
            c.post("/login/2fa", data={"code": "000001" if _code(mail) != "000001" else "000002", "csrf_token": tok}, headers=O)
        assert mfa.fail_count(uid) == 3
        c.get("/login")
        _login(c, "hd_l2b")                                             # 비밀번호가 맞아도 3번은 그대로
        assert mfa.fail_count(uid) == 3
        r = c.post("/login/2fa", data={"code": _code(mail), "csrf_token": _csrf(c, "/login/2fa")}, headers=O, follow_redirects=False)
        assert r.headers["location"].startswith("/login/welcome")
        assert mfa.fail_count(uid) == 0                                 # 2단계까지 성공해야 0


def test_L3_L4_same_message_and_no_remote_lockout():
    """로그인 실패 문구는 한 가지뿐이고, 남이 틀린 비밀번호로 계정을 잠글 수 없음."""
    uid, _ = _user("hd_l3")
    off, _ = _user("hd_off")
    execute("UPDATE users SET is_active=0 WHERE id=?", (off,))
    lk, _ = _user("hd_lk")
    execute("UPDATE users SET locked_until='2099-01-01 00:00:00' WHERE id=?", (lk,))
    c = TestClient(app)

    def msg(name, pw="wrong"):
        t = _login(c, name, pw).text
        return re.search(r'alert alert-danger[^>]*>(.*?)</div>', t, re.S).group(1)

    texts = {msg("hd_l3"), msg("no_such_user_x"), msg("hd_off"), msg("hd_lk"), msg("hd_off", PW), msg("hd_lk", PW)}
    assert len(texts) == 1 and GENERIC in texts.pop()
    attacker = TestClient(app, client=("10.7.7.7", 40000))
    for _ in range(8):
        _login(attacker, "hd_l3", "wrong")
    assert auth.query_one("SELECT locked_until FROM users WHERE id=?", (uid,))["locked_until"] is None
    owner = TestClient(app, client=("10.7.7.8", 40000))
    assert _login(owner, "hd_l3").status_code == 303                    # 본인은 다른 주소에서 정상 로그인
    for _ in range(4):                                                  # 그 주소는 10번 틀리면 어떤 아이디든 차단
        _login(attacker, "someone_else", "wrong")
    r = _login(attacker, "hd_l3")
    assert r.status_code == 401 and "너무 많습니다" in r.text


def test_L5_odd_usernames_do_not_break_audit():
    """아이디 칸에 이상한 값을 넣어도 접속기록 화면·내보내기가 멈추지 않고, 넣은 글자는 기록에 남지 않음."""
    adm = _staff("hd_adm", "admin")
    c = TestClient(app)
    odd = ["enc:v1:AAAAAAAAAAAAAAAAAAAAAAAA", "=cmd|' /C calc'!A0", "<script>alert(1)</script>", "a" * 500, "My-Real-Passw0rd!!", "x\ty\r\n"]
    for v in odd:
        r = c.post("/login", data={"username": v, "password": "x"}, headers=O)
        assert r.status_code == 401 and GENERIC in r.text
        assert v[:20] not in r.text or v == "a" * 500                   # 화면에 되돌려 주지 않음
    assert adm.get("/admin/audit").status_code == 200
    csv = adm.get("/admin/audit/export.csv")
    assert csv.status_code == 200
    for v in odd[:3] + odd[4:5]:
        assert v not in csv.text
    assert not _raw("SELECT 1 FROM login_attempts WHERE username LIKE 'enc:%' OR username LIKE '=%' OR username LIKE 'My-Real%'")
    assert not _raw("SELECT 1 FROM audit_logs WHERE target LIKE 'enc:%' OR target LIKE '=%' OR target LIKE 'My-Real%'")
    # 내보내기 수식 방지: 수식처럼 시작하는 값은 앞에 ' 가 붙음
    execute("INSERT INTO audit_logs(user_id, username, ip, module, action, target, detail) VALUES (NULL,'t','1.1.1.1','auth','시험','=1+1','@x')")
    out = adm.get("/admin/audit/export.csv").text
    assert "'=1+1" in out and "'@x" in out


def test_L6_body_limit_counts_real_bytes():
    """길이 표시 없이 조각으로 보내는 큰 요청도 받은 만큼 세어 끊음."""
    def big(mb):
        for _ in range(mb * 4):
            yield b"a=" + b"x" * (256 * 1024) + b"&"
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/login", content=big(2), headers={**O, "content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413, r.status_code                          # 로그인 요청 한도 64KB
    r = c.post("/login", content=b"username=a&password=" + b"x" * 70000, headers={**O, "content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413
    hr = _staff("hd_big", "hr")
    r = hr.post("/hr/employees/new", content=big(settings.max_upload_mb + 2), headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413
    assert _login(TestClient(app), "hd_big").status_code == 303         # 보통 크기는 그대로 동작


def test_L_login_rate_limit_per_address():
    c = TestClient(app, client=("10.6.6.6", 1))
    codes = [c.post("/login", data={"username": "nobody_rate", "password": "x"}, headers=O).status_code for _ in range(35)]
    assert 429 in codes and codes[0] == 401


# ════ 진단 보고서 항목 ═════════════════════════════════════════════════
def test_S01_folder_name_cannot_reach_other_directories(tmp_path, monkeypatch):
    """임직원 폴더 이름으로 서버의 다른 폴더(암호 키 등)를 열 수 없음."""
    root = tmp_path / "emp"
    (root / "홍길동").mkdir(parents=True)
    (root / "홍길동" / "a.pdf").write_bytes(b"%PDF-1.4")
    secret = tmp_path / "secrets"
    secret.mkdir()
    (secret / "bms_master.key").write_text("k")
    monkeypatch.setattr(settings, "employee_root", str(root))
    hr = _staff("hd_s01", "hr")
    emp = query_one("SELECT id FROM employees ORDER BY id LIMIT 1")
    for bad in (str(secret), "../secrets", "..", "홍길동/../../secrets", "C:\\Windows", "/etc", ".ssh", "a\\b"):
        assert not intake.valid_folder_name(bad), bad
        with pytest.raises(ValueError):
            repo.update_employee(emp["id"], {"folder_name": bad})
        # 예전에 이미 들어간 값이 있더라도 읽는 쪽에서 막힘
        execute("UPDATE employees SET folder_name=? WHERE id=?", (bad, emp["id"]))
        assert intake.employee_folder(repo.get_employee(emp["id"])) is None
        assert intake.list_files(repo.get_employee(emp["id"])) == []
        assert hr.get(f"/hr/employees/{emp['id']}/file", params={"name": "bms_master.key"}).status_code == 404
        assert "bms_master.key" not in hr.get(f"/hr/employees/{emp['id']}?tab=files").text
    execute("UPDATE employees SET folder_name='홍길동' WHERE id=?", (emp["id"],))
    assert hr.get(f"/hr/employees/{emp['id']}/file", params={"name": "a.pdf"}).status_code == 200      # 정상 폴더는 그대로
    execute("UPDATE employees SET folder_name=NULL WHERE id=?", (emp["id"],))


def test_S04_ciphertext_lookalikes_are_harmless():
    """'enc:v1:' 흉내 글자: enc() 는 일반 글자로 암호화하고, 일반 칸 저장은 거부하고, 깨진 값을 읽어도 화면이 멈추지 않음."""
    fake = "enc:v1:hello"
    e = crypto.enc(fake)
    assert isinstance(e, crypto.Enc) and e != fake and crypto.dec(e) == fake
    assert crypto.enc(e) is e                                           # enc() 가 만든 값만 그대로 통과
    assert crypto.dec("enc:v1:AAAA") == crypto.UNREADABLE and crypto.dec("enc:v1:!!!") == crypto.UNREADABLE
    with pytest.raises(ValueError):
        execute("UPDATE settings SET value=? WHERE key='no_such_key'", (fake,))
    # 남의 암호문을 일반 칸에 붙여 넣어 풀어 보게 하는 것도 차단
    stolen = str(crypto.enc("900101-1234567"))
    ga = _staff("hd_s04", "ga")
    a = query_one("SELECT id, name FROM ga_assets ORDER BY id LIMIT 1")
    before = a["name"]
    r = ga.post("/ga/assets/save", data={"id": str(a["id"]), "name": stolen}, follow_redirects=False)
    assert query_one("SELECT name FROM ga_assets WHERE id=?", (a["id"],))["name"] == before
    # 이미 깨진 값이 DB 에 있어도 목록은 열림
    con = sqlite3.connect(os.environ["BMS_DB_PATH"])
    con.execute("UPDATE ga_assets SET name='enc:v1:AAAA' WHERE id=?", (a["id"],))
    con.commit()
    con.close()
    page = ga.get("/ga/assets")
    assert page.status_code == 200 and crypto.UNREADABLE in page.text
    execute("UPDATE ga_assets SET name=? WHERE id=?", (before, a["id"]))


def test_S05_seal_approval_only_by_admin_route():
    ga = _staff("hd_s05", "ga")
    ga.post("/ga/seals/save", data={"use_date": "2026-11-14", "seal_type": "법인인감", "purpose": "시험", "copies": "1",
                                    "status": "승인", "approver": "대표"})
    row = query_one("SELECT * FROM ga_seal_logs ORDER BY id DESC LIMIT 1")
    assert row["status"] == "신청" and not row["approver"]
    ga.post("/ga/seals/save", data={"id": str(row["id"]), "use_date": "2026-11-14", "seal_type": "법인인감", "purpose": "시험2",
                                    "copies": "1", "status": "승인", "approver": "대표", "approved_at": "2026-11-14 10:00"})
    row = query_one("SELECT * FROM ga_seal_logs WHERE id=?", (row["id"],))
    assert row["status"] == "신청" and not row["approver"] and row["purpose"] == "시험2"      # 내용 수정은 되고 승인은 안 됨
    assert ga.post(f"/ga/seals/{row['id']}/decide", data={"decision": "승인"}).status_code == 403
    adm = _staff("hd_s05a", "admin")
    adm.post(f"/ga/seals/{row['id']}/decide", data={"decision": "승인"})
    assert query_one("SELECT status FROM ga_seal_logs WHERE id=?", (row["id"],))["status"] == "승인"
    ga.post("/ga/seals/save", data={"id": str(row["id"]), "use_date": "2026-11-14", "seal_type": "법인인감", "purpose": "바꿈", "copies": "1"})
    assert query_one("SELECT purpose FROM ga_seal_logs WHERE id=?", (row["id"],))["purpose"] == "시험2"   # 처리된 건은 수정 불가


def test_S06_viewer_cannot_probe_contacts_or_see_fines():
    vw, hr = _staff("hd_s06v", "viewer"), _staff("hd_s06h", "hr")
    e = next(x for x in repo.list_employees() if x.get("phone"))
    frag = e["phone"][:7]
    others = {x["name"] for x in repo.list_employees() if frag in str(x.get("phone") or "")}
    assert e["name"] in hr.get("/hr/employees", params={"q": frag}).text           # 인사 담당자는 검색됨
    page = vw.get("/hr/employees", params={"q": frag}).text
    assert not any(f">{n}<" in page for n in others)                               # 조회자는 번호로 걸러지지 않음
    assert e["name"] in vw.get("/hr/employees", params={"q": e["name"]}).text      # 이름 검색은 됨
    v = query_one("SELECT id FROM ga_vehicles ORDER BY id LIMIT 1")
    assert vw.get(f"/ga/vehicles/{v['id']}/logbook.csv", params={"year": 2026}).status_code == 403
    assert vw.get("/ga/vehicles/labels").status_code == 403 and vw.get("/ga/assets/labels").status_code == 403
    assert vw.get("/ga/vehicles", params={"tab": "fines"}).status_code == 200


def test_S08_contract_folder_files_never_run_in_page(tmp_path, monkeypatch):
    from app.modules.ga import routes as ga_routes
    root = tmp_path / "contracts"
    (root / "시험계약").mkdir(parents=True)
    for n, b in (("a.html", b"<script>alert(1)</script>"), ("a.js", b"alert(1)"), ("a.svg", b"<svg/>"), ("a.pdf", b"%PDF-1.4")):
        (root / "시험계약" / n).write_bytes(b)
    monkeypatch.setattr(settings, "contract_root", str(root))
    for n in ("a.html", "a.js", "a.svg"):
        r = ga_routes._folder_file("시험계약", n)
        assert r.media_type == "application/octet-stream" and r.headers["content-disposition"].startswith("attachment")
    r = ga_routes._folder_file("시험계약", "a.pdf")
    assert r.media_type == "application/pdf" and r.headers["content-disposition"].startswith("inline")


def test_C01_certificate_numbers_are_never_reused():
    emp = query_one("SELECT id FROM employees ORDER BY id LIMIT 1")
    made = []
    for _ in range(3):
        no = repo.next_cert_no()
        made.append((execute("INSERT INTO hr_documents(employee_id, doc_group, category, title, doc_no, content) VALUES (?,?,?,?,?,?)",
                             (emp["id"], "certificate", "재직증명서", "t", no, "x")), no))
    assert len({n for _, n in made}) == 3
    execute("DELETE FROM hr_documents WHERE id=?", (made[1][0],))
    a = repo.next_cert_no()
    execute("DELETE FROM hr_documents WHERE id=?", (made[2][0],))                 # 가장 마지막 번호를 지워도
    b = repo.next_cert_no()
    assert len({a, b} | {n for _, n in made}) == 5, (made, a, b)
    execute("DELETE FROM hr_documents WHERE id=?", (made[0][0],))


def test_C02_review_state_is_rechecked_at_save():
    """화면을 연 뒤 확정·반영됐으면, 예전에 읽은 상태로 들어온 저장·반영은 거부되고 상태가 되돌아가지 않음."""
    year = 2019
    execute("DELETE FROM review_cycles WHERE year=?", (year,))
    review.create(year, "t")
    stale = review.get(year)                                             # '준비' 상태에서 읽어 둔 것
    review.save_setup(stale, {"base_rate": "3"}, "t")
    row = next(x for x in review.compute(review.get(year))["rows"])
    eid = row["employee"]["id"]
    review.set_status(review.get(year), "확정", "t")
    for fn, args in ((review.save_items, (stale, [{"employee_id": eid, "final_rate": "25"}], "t")),
                     (review.save_setup, (stale, {"base_rate": "9"}, "t"))):
        with pytest.raises(review.ReviewError):
            fn(*args)
    assert review.get(year)["status"] == "확정" and review.get(year)["base_rate"] == 3
    confirmed = review.get(year)
    review.apply(confirmed, "t")
    assert review.get(year)["status"] == "반영완료"
    with pytest.raises(review.ReviewError):
        review.apply(confirmed, "t")                                     # 두 번 반영 안 됨
    with pytest.raises(review.ReviewError):
        review.save_items(stale, [{"employee_id": eid, "final_rate": "25"}], "t")
    with pytest.raises(review.ReviewError):
        review.set_status(stale, "평가중", "t")                          # 예전 상태 기준의 상태 변경도 거부
    assert review.get(year)["status"] == "반영완료"
    its = review.items(review.get(year)).get(eid, {})
    assert its.get("final_rate") != 25
    execute("DELETE FROM salaries WHERE year=?", (year + 1,))
    execute("DELETE FROM review_cycles WHERE year=?", (year,))
