"""같은 서버의 HTTPS 프록시(Tailscale serve 등)를 거친 요청: 쿠키 Secure·HSTS·출처 확인·접속 IP.
사내 직접 http 접속은 예전처럼 Secure 없이 동작해야 함."""
import re

from fastapi.testclient import TestClient

from app.main import app
from app.security import auth

PW = "Px-Strong-Pass-1!"
EXT = "vm1.example.ts.net"


def _user():
    if not auth.query_one("SELECT 1 FROM users WHERE username='px_hr'"):
        auth.create_user("px_hr", "px_hr", "hr", PW, must_change=False)


def _cookies(r):
    return [v for k, v in r.headers.multi_items() if k.lower() == "set-cookie"]


def test_direct_http_unchanged():
    with TestClient(app, base_url="http://testserver") as c:
        _user()
        r = c.post("/login", data={"username": "px_hr", "password": PW}, headers={"origin": "http://testserver"},
                   follow_redirects=False)
        assert r.status_code in (302, 303)
        ck = _cookies(r)
        assert ck and all("secure" not in v.lower() for v in ck)
        assert "strict-transport-security" not in r.headers


def test_https_proxy_from_localhost():
    with TestClient(app, base_url=f"http://{EXT}", client=("127.0.0.1", 50123)) as c:
        _user()
        prox = {"x-forwarded-proto": "https", "x-forwarded-for": "100.64.1.2", "x-forwarded-host": EXT}
        r = c.post("/login", data={"username": "px_hr", "password": PW},
                   headers={**prox, "origin": f"https://{EXT}"}, follow_redirects=False)
        assert r.status_code in (302, 303), r.text[:300]
        ck = _cookies(r)
        assert ck and all("secure" in v.lower() for v in ck)
        assert r.headers.get("strict-transport-security")
        row = auth.query_one("SELECT ip FROM sessions ORDER BY rowid DESC LIMIT 1") if _has_ip() else None
        if row:
            assert row["ip"] == "100.64.1.2"
        # 다른 사이트에서 온 요청은 여전히 차단
        r = c.post("/login", data={"username": "px_hr", "password": PW},
                   headers={**prox, "origin": "https://evil.example"}, follow_redirects=False)
        assert r.status_code == 403


def test_forwarded_headers_ignored_from_outside():
    """사내 다른 PC가 X-Forwarded-* 를 꾸며 보내도 무시."""
    with TestClient(app, base_url="http://testserver", client=("192.168.12.50", 50000)) as c:
        _user()
        r = c.post("/login", data={"username": "px_hr", "password": PW},
                   headers={"origin": "http://testserver", "x-forwarded-proto": "https", "x-forwarded-for": "1.2.3.4"},
                   follow_redirects=False)
        assert r.status_code in (302, 303)
        assert all("secure" not in v.lower() for v in _cookies(r))


def _has_ip():
    cols = [r["name"] for r in auth.query("PRAGMA table_info(sessions)")]
    return "ip" in cols
