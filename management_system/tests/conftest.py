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
    "BMS_REQUIRE_2FA": "false",
    "BMS_INITIAL_ADMIN_PASSWORD": "Init-Admin-Pw1!",
    "BMS_CONFIG": os.path.join(_tmp, "none.toml"),
})
if os.path.exists("/opt/pw-browsers/chromium"):
    import glob
    exe = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome"))
    if exe:
        os.environ["BMS_PDF_BROWSER_PATH"] = exe[-1]
