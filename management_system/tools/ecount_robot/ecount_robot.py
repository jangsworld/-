"""BMS → 이카운트 사원등록 로봇 (관리 PC 에서 실행).

    python ecount_robot.py setup    처음 한 번: 이카운트 계정·BMS 주소·로봇 열쇠를 직접 입력 (이 PC 에 암호화 보관)
    python ecount_robot.py login    로그인만 시험 (추가 인증이 필요하면 열린 창에서 직접)
    python ecount_robot.py depts    이카운트 부서 목록을 읽어 BMS 로 보냄 (부서 짝 맞추기 표에 쓰임)
    python ecount_robot.py once     BMS 에 요청이 있으면 한 건씩 처리하고 끝 (예약 작업이 몇 분마다 실행)

- 비밀번호·열쇠는 Windows 가 이 PC·이 사용자만 풀 수 있게 암호화(DPAPI)해 둡니다. 화면·기록에 찍지 않습니다.
- 값을 넣고 저장한 뒤 그 사원을 다시 열어 한 칸씩 대조하고, 결과(칸 이름만)를 BMS 에 돌려줍니다.
- 처음 보는 안내 창·추가 인증·보안문자가 나오면 넘기려 하지 않고 멈춰서 알립니다.
"""
import base64
import ctypes
import ctypes.wintypes as wt
import getpass
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

HOME = os.environ.get("BMS_ECOUNT_ROBOT_HOME") or r"C:\BMS_local\ecount_robot"
SECRET = os.path.join(HOME, "secret.json")
PROFILE = os.path.join(HOME, "profile_chrome")
LOGS = os.path.join(HOME, "logs")
LOGIN_URL = "https://login.ecount.com/Login/"
MAX_JOBS_PER_RUN = 10


# ── 암호화 보관 (Windows DPAPI, 현재 사용자) ──
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    src = _Blob(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("암호화 보관 값을 다룰 수 없습니다 (다른 PC·다른 Windows 계정에서는 풀리지 않습니다)")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def enc(text: str) -> str:
    return base64.b64encode(_dpapi(text.encode("utf-8"), True)).decode()


def dec(text: str) -> str:
    return _dpapi(base64.b64decode(text), False).decode("utf-8")


def load_secret() -> dict:
    try:
        with open(SECRET, encoding="utf-8") as f:
            s = json.load(f)
    except (OSError, ValueError):
        raise SystemExit("설정이 없습니다. 먼저 'setup' 을 실행하세요.")
    return {"com_code": s["com_code"], "user_id": s["user_id"], "password": dec(s["password"]),
            "bms_url": s["bms_url"].rstrip("/"), "robot_key": dec(s["robot_key"])}


def log(msg: str):
    os.makedirs(LOGS, exist_ok=True)
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    with open(os.path.join(LOGS, f"robot_{datetime.now():%Y%m}.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ── BMS 와 통신 ──
def bms(sec: dict, path: str, body: dict) -> dict:
    req = urllib.request.Request(sec["bms_url"] + path, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "X-Robot-Key": sec["robot_key"], "Origin": sec["bms_url"]})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


# ── 이카운트 ──
class NeedHuman(Exception):
    """사람이 직접 해야 하는 일 (추가 인증 등)."""


def open_browser(pw, show: bool = True):
    os.makedirs(PROFILE, exist_ok=True)
    args = ["--start-maximized"] if show else ["--window-position=-32000,-32000"]
    return pw.chromium.launch_persistent_context(PROFILE, channel="chrome", headless=False, no_viewport=True, args=args)


def _inside(url: str) -> bool:
    u = url.split("?")[0].lower()
    return "ecount.com" in u and "/view/erp" in u and "erp_login" not in u


def ensure_login(page, sec: dict, wait_human_sec: int = 0):
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    if not _inside(page.url):
        page.fill("#com_code", sec["com_code"])
        page.fill("#id", sec["user_id"])
        page.fill("#passwd", sec["password"])
        page.click("button#save")
    end = time.time() + 40
    while time.time() < end:
        page.wait_for_timeout(1000)
        if _inside(page.url):
            page.wait_for_timeout(2500)
            return
    text = page.inner_text("body")[:2000]
    if re.search(r"인증|보안문자|자동입력", text):
        if wait_human_sec:
            log(f"추가 인증이 필요합니다. 열린 창에서 직접 마쳐 주세요 (최대 {wait_human_sec // 60}분 기다립니다)")
            end = time.time() + wait_human_sec
            while time.time() < end:
                page.wait_for_timeout(2000)
                if _inside(page.url):
                    page.wait_for_timeout(2500)
                    return
        raise NeedHuman("이카운트가 추가 인증을 요구합니다. 관리 PC에서 'login' 을 실행해 한 번 직접 인증해 주세요.")
    if re.search(r"비밀번호|아이디|회사코드", text) and "일치" in text:
        raise NeedHuman("이카운트 로그인 정보가 맞지 않습니다. 'setup' 으로 다시 넣어 주세요.")
    raise NeedHuman("이카운트 로그인 화면을 넘어가지 못했습니다 (화면이 바뀌었거나 안내 창이 떴을 수 있습니다).")


def shot(page, name: str):
    """문제가 생겼을 때만 이 PC 에 화면을 남김 (BMS 로 보내지 않음, 30일 뒤 삭제)."""
    try:
        os.makedirs(LOGS, exist_ok=True)
        page.screenshot(path=os.path.join(LOGS, f"{datetime.now():%Y%m%d_%H%M%S}_{name}.png"))
        limit = time.time() - 30 * 86400
        for f in os.listdir(LOGS):
            p = os.path.join(LOGS, f)
            if f.endswith(".png") and os.path.getmtime(p) < limit:
                os.remove(p)
    except Exception:       # noqa: BLE001
        pass


def do_job(page, job: dict) -> tuple[bool, str, dict]:
    import ecount_form as ef
    v, action = job["values"], job["action"]
    f = ef.EmployeeForm(page, log=lambda m: log(m))
    f.ok_confirms = job.get("ok_confirms") or []
    detail = {"saved": False, "existed": False, "skipped": [], "mismatch": [], "confirmed": []}
    f.open_list()
    exists = f.find(v["emp_no"])
    if action == "신규":
        keys = None
        if exists:
            detail["existed"] = True
            log(f"   · {v['emp_no']} 은(는) 이미 이카운트에 있어 새로 넣지 않고 대조만 합니다")
        else:
            f.open_new()
            f.fill_new(v)
            msg = f.save()
            detail["skipped"], detail["confirmed"] = [list(x) for x in f.skipped], f.confirmed
            if msg:
                shot(page, "save_blocked")
                f.close_dialogs()
                return False, "이카운트가 저장을 받지 않았습니다: " + msg[:200], detail
    else:
        if not exists:
            return False, f"이카운트에 사원번호 {v['emp_no']} 이(가) 없어 퇴사일을 넣지 못했습니다", detail
        keys = ["resign_date"]
        f.open_existing(v["emp_no"])
        f.fill_resign(v)
        msg = f.save()
        detail["skipped"], detail["confirmed"] = [list(x) for x in f.skipped], f.confirmed
        if msg:
            shot(page, "save_blocked")
            f.close_dialogs()
            return False, "이카운트가 저장을 받지 않았습니다: " + msg[:200], detail
    page.wait_for_timeout(1500)
    g = ef.EmployeeForm(page, log=lambda m: log(m))
    g.open_existing(v["emp_no"])
    got = g.read_back()
    g.close_dialogs()
    skipped_labels = {a for a, _b in detail["skipped"]}
    detail["mismatch"] = [m for m in ef.compare(v, got, keys) if m not in skipped_labels]
    detail["saved"] = True
    if detail["mismatch"]:
        shot(page, "mismatch")
        head = "이미 있는 사원인데 값이 다릅니다" if detail["existed"] else "저장은 됐지만 값이 다른 칸이 있습니다"
        return False, head + " (이카운트에서 확인하세요)", detail
    return True, "이미 등록되어 있고 값이 같습니다" if detail["existed"] else "저장하고 대조까지 마쳤습니다", detail


def read_depts(page) -> list[list[str]]:
    import ecount_form as ef
    f = ef.EmployeeForm(page, log=lambda m: None)
    f.open_list()
    f.open_new()
    try:
        return f.list_depts()
    finally:
        f.close_dialogs()


# ── 명령 ──
def cmd_setup():
    print("이카운트 로봇 설정 — 입력한 값은 이 PC 에 암호화해 보관합니다 (화면에 보이지 않는 칸은 그대로 입력 후 Enter).")
    old = {}
    try:
        with open(SECRET, encoding="utf-8") as f:
            old = json.load(f)
    except (OSError, ValueError):
        pass

    def ask(label, key, secret=False):
        if secret:
            v = getpass.getpass(f"{label}{' (Enter = 그대로 둠)' if old.get(key) else ''}: ").strip()
            return enc(v) if v else old.get(key, "")
        v = input(f"{label}{' [' + old[key] + ']' if old.get(key) else ''}: ").strip()
        return v or old.get(key, "")
    s = {"com_code": ask("이카운트 회사코드", "com_code"), "user_id": ask("이카운트 아이디 (로봇 전용 계정)", "user_id"),
         "password": ask("이카운트 비밀번호", "password", True), "bms_url": ask("BMS 주소 (https:// 로 시작)", "bms_url").rstrip("/"),
         "robot_key": ask("BMS 로봇 열쇠 (BMS › 이카운트 연동 › 로봇 탭에서 만든 값)", "robot_key", True)}
    if not all(s.values()):
        raise SystemExit("빈 값이 있습니다. 다시 실행해 주세요.")
    if not s["bms_url"].lower().startswith("https://"):
        raise SystemExit("BMS 주소는 https:// 로 시작해야 합니다 (주민번호가 오가므로 암호화된 주소만 씁니다).")
    os.makedirs(HOME, exist_ok=True)
    with open(SECRET, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)
    print("저장했습니다:", SECRET)
    try:
        r = bms(load_secret(), "/robot/ecount/claim.check", {})
    except urllib.error.HTTPError as ex:
        r = {"status": ex.code}
    except Exception as ex:      # noqa: BLE001
        r = {"error": type(ex).__name__}
    print("BMS 연결 확인:", "주소에 닿습니다" if r.get("status") in (403, 404, 405) or r.get("ok") is not None else f"닿지 않습니다 {r}")


def with_browser(fn, show=True, wait_human_sec=0):
    from playwright.sync_api import sync_playwright
    sec = load_secret()
    with sync_playwright() as pw:
        try:
            ctx = open_browser(pw, show)
        except Exception as ex:      # noqa: BLE001
            raise SystemExit(f"크롬 창을 열지 못했습니다 (로봇용 크롬 창이 이미 열려 있으면 닫아 주세요): {type(ex).__name__}")
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            ensure_login(page, sec, wait_human_sec)
            return fn(page, sec)
        finally:
            ctx.close()


def cmd_login():
    with_browser(lambda page, sec: log("이카운트 로그인 성공"), show=True, wait_human_sec=600)


def cmd_depts():
    def run(page, sec):
        rows = read_depts(page)
        r = bms(sec, "/robot/ecount/depts", {"depts": rows})
        log(f"이카운트 부서 {len(rows)}개를 읽어 BMS 로 보냈습니다 (저장 {r.get('saved')})")
    with_browser(run)


def cmd_once(show=True):
    sec = load_secret()
    try:
        first = bms(sec, "/robot/ecount/claim", {}).get("job")
    except urllib.error.HTTPError as ex:
        log(f"BMS 가 거절했습니다 ({ex.code}). 로봇 열쇠·주소를 확인하세요.")
        return 2
    except Exception as ex:      # noqa: BLE001
        log(f"BMS 에 닿지 못했습니다: {type(ex).__name__}")
        return 2
    if not first:
        return 0

    def run(page, sec):
        job, n = first, 0
        while job and n < MAX_JOBS_PER_RUN:
            n += 1
            emp = job["values"].get("emp_no")
            log(f"일감 #{job['job_id']} {job['action']} {emp} 시작")
            try:
                ok, msg, detail = do_job(page, job)
            except Exception as ex:      # noqa: BLE001
                shot(page, "error")
                ok, msg, detail = False, f"로봇 오류: {type(ex).__name__} — 이카운트 화면이 바뀌었을 수 있습니다", {"saved": False}
                try:
                    import ecount_form as ef
                    ef.EmployeeForm(page).close_dialogs()
                except Exception:        # noqa: BLE001
                    pass
            log(f"일감 #{job['job_id']} {'완료' if ok else '실패'}: {msg} · 못 넣은 칸 {len(detail.get('skipped') or [])} · 다른 칸 {len(detail.get('mismatch') or [])}")
            bms(sec, "/robot/ecount/finish", {"job_id": job["job_id"], "ok": ok, "message": msg, "detail": detail})
            job = bms(sec, "/robot/ecount/claim", {}).get("job")
    try:
        with_browser(run, show=show)
    except NeedHuman as ex:
        log(str(ex))
        bms(sec, "/robot/ecount/finish", {"job_id": first["job_id"], "ok": False, "message": str(ex), "detail": {"saved": False}})
        return 3
    except SystemExit as ex:
        log(str(ex))
        bms(sec, "/robot/ecount/finish", {"job_id": first["job_id"], "ok": False, "message": str(ex)[:200], "detail": {"saved": False}})
        return 3
    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "setup":
        cmd_setup()
    elif cmd == "login":
        cmd_login()
    elif cmd == "depts":
        cmd_depts()
    elif cmd == "once":
        sys.exit(cmd_once(show="--hide" not in sys.argv))
    else:
        print(__doc__)
