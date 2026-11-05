"""중복 로그인 차단, 실사용 전환 명령 테스트."""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import manage
from app.config import settings
from app.db import execute, get_codes, get_settings, query, query_one
from app.main import app
from app.modules.ga import repo as ga_repo
from app.security import auth

ORIGIN = {"origin": "http://testserver"}
PASSWORD = "Strong-Pass-123!"


def _login(user):
    c = TestClient(app, follow_redirects=False)
    c.post("/login", data={"username": user, "password": PASSWORD}, headers=ORIGIN)
    return c


def _ensure(user):
    if not query_one("SELECT 1 x FROM users WHERE username=?", (user,)):
        auth.create_user(user, user, "viewer", PASSWORD, must_change=False)


def test_second_login_ends_first_session(monkeypatch):
    monkeypatch.setattr(settings, "require_2fa", False)
    _ensure("dup_user")
    _ensure("other_user")
    first, other = _login("dup_user"), _login("other_user")
    assert first.get("/hr/dashboard").status_code == 200

    second = _login("dup_user")
    assert second.get("/hr/dashboard").status_code == 200
    kicked = first.get("/hr/dashboard")
    assert kicked.status_code == 303 and "ended=1" in kicked.headers["location"]
    assert "다른 곳에서 로그인" in first.get(kicked.headers["location"]).text
    assert other.get("/hr/dashboard").status_code == 200                # 다른 계정은 영향 없음


def test_password_ok_without_otp_keeps_existing_session(monkeypatch):
    _ensure("otp_user")
    monkeypatch.setattr(settings, "require_2fa", False)
    first = _login("otp_user")
    uid = query_one("SELECT id FROM users WHERE username='otp_user'")["id"]
    execute("UPDATE users SET totp_enabled=1, totp_secret='' WHERE id=?", (uid,))
    _login("otp_user")                                                  # OTP 미입력: 기존 접속 유지
    assert first.get("/hr/dashboard").status_code == 200
    execute("UPDATE users SET totp_enabled=0 WHERE id=?", (uid,))


def test_prepare_production_requires_flag_and_keeps_accounts(tmp_path, monkeypatch):
    conn = sqlite3.connect(settings.db_path)
    before = conn.execute("SELECT * FROM doc_templates").fetchall()
    codes = conn.execute("SELECT group_code, name, sort_order, behavior FROM codes").fetchall()
    conn.close()
    users = len(query("SELECT id FROM users"))
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps({
        "settings": {"company_name": "테스트회사"},
        "codes": [{"group_code": "DEPT", "name": "경영관리부", "sort_order": 1, "behavior": None}],
        "templates": [{"doc_group": "계약", "category": "근로계약서", "name": "회사양식", "content": "<p>본문</p>", "is_active": 1}],
    }, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr("sys.argv", ["manage.py", "prepare-production", str(seed)])
    with pytest.raises(SystemExit):
        manage.main()

    backup = tmp_path / "keep.db"
    src, dst = sqlite3.connect(settings.db_path), sqlite3.connect(backup)
    src.backup(dst)
    dst.close()
    monkeypatch.setattr("sys.argv", ["manage.py", "prepare-production", str(seed), "--delete-all-business-data"])
    manage.main()
    assert query("SELECT COUNT(*) c FROM employees")[0]["c"] == 0 and not ga_repo.list_vehicles()
    assert get_settings()["company_name"] == "테스트회사" and get_codes("DEPT") == ["경영관리부"]
    assert query("SELECT name FROM doc_templates") == [{"name": "회사양식"}] and len(query("SELECT id FROM users")) == users

    restore = sqlite3.connect(backup)                                   # 다음 테스트를 위해 되돌림
    restore.backup(src)
    restore.close()
    src.close()
    assert len(before) and len(codes)
