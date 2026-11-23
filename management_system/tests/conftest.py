import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_tmp = tempfile.mkdtemp()
os.environ.update({
    "BMS_DB_PATH": os.path.join(_tmp, "test.db"),
    "BMS_SECRETS_DIR": os.path.join(_tmp, "secrets"),
    "BMS_LOG_DIR": os.path.join(_tmp, "logs"),
    "BMS_NAS_ROOT": os.path.join(_tmp, "nas"),
    "BMS_SEED_SAMPLE": "true",
    "BMS_EMPLOYEE_DRAFTS": "true",          # 꺼 둔 기능(입사서류 초안)의 코드도 계속 시험
    "BMS_REQUIRE_2FA": "false",
    "BMS_INITIAL_ADMIN_PASSWORD": "Init-Admin-Pw1!",
    "BMS_CONFIG": os.path.join(_tmp, "none.toml"),
})
if os.path.exists("/opt/pw-browsers/chromium"):
    import glob
    exe = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome"))
    if exe:
        os.environ["BMS_PDF_BROWSER_PATH"] = exe[-1]


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_login_limits():
    """시험은 모두 같은 주소에서 오므로, 시험마다 로그인 제한(주소당 요청 수·실패 기록)을 비움.
    한 시험 안에서는 제한이 실제와 똑같이 동작합니다."""
    from app.security import middleware
    middleware._login_hits.clear()
    try:
        from app.db import execute
        execute("DELETE FROM login_attempts WHERE success=0")
    except Exception:            # noqa: BLE001  (DB 가 아직 없는 첫 시험)
        pass
    yield
