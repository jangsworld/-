"""BMS → 이카운트 사원등록 입력 도우미.

담당자 PC 에서 실행합니다. Edge 창을 하나 열고, 담당자가 BMS 와 이카운트에 '직접' 로그인하면
BMS 의 '이카운트 반영 대기' 직원 정보를 이카운트 화면의 칸에 채웁니다. 저장 버튼은 누르지 않습니다.

- 이카운트·BMS 비밀번호를 묻거나 저장하지 않습니다. 로그인 상태는 이 PC 의 도우미 전용 브라우저 프로필에만 남습니다.
- 칸은 화면에 보이는 글자(예: '성명', '입사일자')로 찾습니다. 이카운트 화면이 바뀌면 못 찾는 칸이 생길 수 있습니다.
"""
import json
import os
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.path.join(os.environ.get("LOCALAPPDATA") or HERE, "BMS-ecount-helper", "profile")
ECOUNT_LOGIN = "https://login.ecount.com/"

FILL_JS = r"""
(fields) => {
  const norm = s => String(s || '').replace(/\((필수|선택)\)|\[(필수|선택)\]|필수/g, '').replace(/[\s\*\(\)\[\]:：·\/_\-~.]/g, '').toLowerCase();
  const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length)) && getComputedStyle(el).visibility !== 'hidden';
  const editable = 'input:not([type=hidden]):not([type=button]):not([type=submit]):not([type=checkbox]):not([type=radio]):not([type=file]):not([readonly]):not([disabled]),select:not([disabled]),textarea:not([readonly]):not([disabled])';
  const labels = [...document.querySelectorAll('th,td,label,span,div,dt,li,p,strong,b,em')]
    .filter(el => visible(el) && el.textContent.trim().length > 0 && el.textContent.trim().length <= 30 && !el.querySelector(editable));
  const used = new Set(window.__bmsUsed || []);
  const done = [];
  const setVal = (el, v) => {
    if (el.tagName === 'SELECT') {
      const opt = [...el.options].find(o => norm(o.text) === norm(v) || norm(o.value) === norm(v))
               || [...el.options].find(o => norm(o.text).includes(norm(v)) && norm(v).length > 0);
      if (!opt) return false;
      el.value = opt.value;
    } else {
      const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      el.focus();
      Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, v);
      el.dispatchEvent(new Event('input', {bubbles: true}));
    }
    el.dispatchEvent(new Event('change', {bubbles: true}));
    el.dispatchEvent(new Event('blur', {bubbles: true}));
    el.style.background = '#fff3a8';
    el.style.outline = '2px solid #f0ad00';
    return true;
  };
  const near = lab => {
    if (lab.tagName === 'LABEL' && lab.htmlFor) { const t = document.getElementById(lab.htmlFor); if (t && visible(t)) return t; }
    let p = lab;
    for (let i = 0; i < 4 && p; i++) {
      p = p.parentElement; if (!p) break;
      const cand = [...p.querySelectorAll(editable)].filter(x => visible(x) && !used.has(x)
        && (lab.compareDocumentPosition(x) & Node.DOCUMENT_POSITION_FOLLOWING));
      if (cand.length) return cand[0];
    }
    return null;
  };
  for (const f of fields) {
    const names = f.aliases.map(norm);
    let hits = labels.filter(el => names.includes(norm(el.textContent)));
    hits.sort((a, b) => names.indexOf(norm(a.textContent)) - names.indexOf(norm(b.textContent)));
    for (const lab of hits) {
      const input = near(lab);
      if (input && setVal(input, f.value)) { used.add(input); done.push(f.key); break; }
    }
  }
  window.__bmsUsed = [...used];
  return done;
}
"""

DUMP_JS = r"""
() => {
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const out = [];
  document.querySelectorAll('input,select,textarea').forEach(el => {
    if (!visible(el) || ['hidden', 'button', 'submit'].includes(el.type)) return;
    let lab = '', p = el;
    for (let i = 0; i < 4 && p && !lab; i++) {
      p = p.parentElement; if (!p) break;
      const prev = p.previousElementSibling; if (prev && prev.textContent.trim().length <= 30) lab = prev.textContent.trim();
    }
    out.push({tag: el.tagName, type: el.type || '', name: el.name || '', id: el.id || '', label: lab});
  });
  return out;
}
"""


def say(msg=""):
    print(msg, flush=True)


def load_config():
    path = os.path.join(HERE, "config.json")
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    if not cfg.get("bms_url"):
        cfg["bms_url"] = input("BMS 주소를 입력하세요 (예: http://서버주소:3102): ").strip().rstrip("/")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg


class Helper:
    def __init__(self, pw, bms_url):
        self.bms = bms_url.rstrip("/")
        os.makedirs(PROFILE, exist_ok=True)
        self.ctx = pw.chromium.launch_persistent_context(PROFILE, channel="msedge", headless=False,
                                                         no_viewport=True, args=["--start-maximized"])
        pages = self.ctx.pages
        self.bms_page = pages[0] if pages else self.ctx.new_page()
        self.bms_page.goto(self.bms + "/hr/ecount")
        self.ec_page = self.ctx.new_page()
        self.ec_page.goto(ECOUNT_LOGIN)
        self.csrf = ""

    # ── BMS ──
    def pending(self):
        r = self.ctx.request.get(self.bms + "/hr/ecount/api/pending", max_redirects=0)
        if r.status != 200 or "json" not in (r.headers.get("content-type") or ""):
            return None
        data = r.json()
        self.csrf = data.get("csrf") or ""
        return data.get("items") or []

    def _post(self, path, body):
        return self.ctx.request.post(self.bms + path, data=json.dumps(body),
                                     headers={"content-type": "application/json", "x-csrf-token": self.csrf,
                                              "origin": self.bms})

    def payload(self, item):
        r = self._post("/hr/ecount/api/payload", {"employee_id": item["employee_id"], "action": item["action"]})
        return r.json() if r.status == 200 else None

    def done(self, item):
        r = self._post("/hr/ecount/api/done", {"employee_id": item["employee_id"], "action": item["action"]})
        return r.status == 200

    # ── 이카운트 ──
    def ecount_page(self):
        """가장 최근에 연 이카운트 탭 (사원등록 창이 새 창으로 뜨는 경우 포함)."""
        pages = [p for p in self.ctx.pages if "ecount" in (p.url or "") and not p.is_closed()]
        return pages[-1] if pages else None

    def fill(self, page, fields):
        filled = set()
        for frame in page.frames:
            todo = [f for f in fields if f["key"] not in filled]
            if not todo:
                break
            try:
                filled.update(frame.evaluate(FILL_JS, todo))
            except Exception:            # 다른 사이트 광고 등 접근 불가 프레임은 건너뜀
                continue
        return filled

    def reset_marks(self, page):
        for frame in page.frames:
            try:
                frame.evaluate("() => { window.__bmsUsed = []; }")
            except Exception:
                pass

    def dump(self, page):
        out = []
        for i, frame in enumerate(page.frames):
            try:
                for row in frame.evaluate(DUMP_JS):
                    out.append({"frame": i, **row})
            except Exception:
                continue
        name = os.path.join(HERE, f"ecount_fields_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        with open(name, "w", encoding="utf-8") as f:
            json.dump({"url": page.url.split("?")[0], "inputs": out}, f, ensure_ascii=False, indent=1)
        return name, len(out)


def run():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        say("playwright 가 설치되지 않았습니다. run_helper.bat 으로 실행하세요.")
        return
    cfg = load_config()
    say("=" * 60)
    say(" BMS → 이카운트 사원등록 입력 도우미")
    say("=" * 60)
    say(" Edge 창이 열립니다. BMS 탭과 이카운트 탭에서 각각 직접 로그인하세요.")
    say(" (저장 버튼은 도우미가 누르지 않습니다. 화면을 확인하고 직접 저장하세요.)")
    with sync_playwright() as pw:
        h = Helper(pw, cfg["bms_url"])
        while True:
            items = h.pending()
            say()
            if items is None:
                input(" BMS 로그인이 필요합니다. BMS 탭에서 로그인한 뒤 Enter ▶ ")
                continue
            if not items:
                say(" 이카운트 반영 대기 직원이 없습니다.")
            for n, it in enumerate(items, 1):
                when = it["hire_date"] if it["action"] == "신규" else it["resign_date"]
                warn = f"  ※ {', '.join(it['warn'])}" if it.get("warn") else ""
                say(f"  {n:>2}. [{it['action']}] {it['name']} ({it['emp_no']}, {it['department']}, {when}){warn}")
            cmd = input("\n 번호 선택 / r 새로고침 / d 화면 칸 목록 저장 / q 끝내기 ▶ ").strip().lower()
            if cmd == "q":
                break
            if cmd in ("", "r"):
                continue
            if cmd == "d":
                page = h.ecount_page()
                if not page:
                    say(" 이카운트 화면을 찾지 못했습니다.")
                    continue
                name, cnt = h.dump(page)
                say(f" 입력칸 {cnt}개 목록을 저장했습니다: {name}")
                say(" (직원 정보는 들어 있지 않습니다. 칸을 못 찾을 때 이 파일을 보내 주세요.)")
                continue
            if not cmd.isdigit() or not 1 <= int(cmd) <= len(items):
                say(" 목록의 번호를 입력하세요.")
                continue
            it = items[int(cmd) - 1]
            data = h.payload(it)
            if not data:
                say(" BMS에서 자료를 받지 못했습니다. BMS 로그인 상태를 확인하세요.")
                continue
            fields = data["fields"]
            screen = "사원등록 [신규] 입력 화면" if it["action"] == "신규" else f"{it['name']} 사원의 [수정] 화면"
            input(f"\n 이카운트에서 {screen}을 연 뒤 Enter ▶ ")
            filled = set()
            page = None
            while True:
                page = h.ecount_page()
                if not page:
                    input(" 이카운트 화면을 찾지 못했습니다. 이 창에서 연 이카운트에서 화면을 연 뒤 Enter ▶ ")
                    continue
                got = h.fill(page, [f for f in fields if f["key"] not in filled])
                filled |= got
                left = [f["label"] for f in fields if f["key"] not in filled]
                say(f" 채운 칸 {len(filled)}/{len(fields)} (노란색 표시)")
                if left:
                    say(" 못 찾은 칸: " + ", ".join(left))
                    say(" → 다른 탭(급여지급사항·기타설정 등)을 연 뒤 Enter = 이어 채우기")
                cmd2 = input(" Enter 이어 채우기 / s 이카운트에 저장했음 / x 그만 ▶ ").strip().lower()
                if cmd2 == "s":
                    if h.done(it):
                        say(f" ✔ {it['name']} — BMS에 '이카운트 반영 완료(도우미)'로 기록했습니다.")
                    else:
                        say(" BMS 기록에 실패했습니다. BMS 화면에서 '반영 완료로 표시'를 눌러 주세요.")
                    break
                if cmd2 == "x":
                    say(" 이 직원은 대기 상태로 둡니다.")
                    break
            if page:
                h.reset_marks(page)
            time.sleep(0.2)
        h.ctx.close()


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        pass
    except Exception as ex:          # 창이 바로 닫혀 오류를 못 보는 일이 없게
        say(f"\n 오류가 났습니다: {ex.__class__.__name__}: {ex}")
    input("\n Enter 를 누르면 창을 닫습니다 ▶ ")
    sys.exit(0)
