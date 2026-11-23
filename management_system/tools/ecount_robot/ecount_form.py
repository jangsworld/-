"""이카운트 「급여관리 › 사원등록」 화면 조작 (Playwright page 를 받아 씀).

화면의 칸은 이카운트가 붙여 둔 고유 이름(data-cid)으로 찾습니다. 화면 글자로 짐작하지 않습니다.
못 넣은 칸은 건너뛰지 않고 skipped 에 이유와 함께 남깁니다. 저장 뒤에는 다시 열어 값을 대조합니다.
"""
import re

LIST = '[data-ecpath^="EPB002M"]'
DLG = 'div.ui-dialog[data-popup-id^="EPB001M"]'
MENU_HASH = "menuType=MENUTREE_000748&menuSeq=MENUTREE_000760&groupSeq=MENUTREE_000749&prgId=E090101&depth=4"

# BMS 값 → 이카운트 칸. (key, 이름, 탭, 종류, cid)
TEXT_FIELDS = [
    ("emp_no", "사원번호", "A1", "EMP_CD"),
    ("name", "성명", "A1", "EMP_KNAME"),
    ("rrn", "주민등록번호", "A1", "JUMIN_NO"),
    ("email", "이메일", "A1", "E_MAIL"),
    ("phone", "휴대폰", "A1", "HP_NO"),
    ("bank_account", "계좌번호", "A1", "BANKACCOUNT_account"),
    ("account_holder", "예금주", "A1", "BANKACCOUNT_input_2"),
    ("address", "주소", "A1", "ADDRESS_areaAddr"),
]
CODE_FIELDS = [("rank", "직급", "JOB_POSITION"), ("title", "직책", "DUTY_CD2")]
INSURANCE = [("insured_monthly", "건강보험 보수월액", "HEALTH_REDUCE"), ("pension_monthly", "국민연금 기준소득월액", "ANNUITY_FLAG"),
             ("employ_monthly", "고용보험 보수월액", "INSURE_GUBUN")]


class FormError(Exception):
    pass


def _n(s) -> str:
    return re.sub(r"\s", "", str(s or ""))


def _digits(s) -> str:
    return re.sub(r"\D", "", str(s or ""))


class EmployeeForm:
    def __init__(self, page, log=print):
        self.page = page
        self.log = log
        self.filled, self.skipped = [], []      # skipped: [(이름, 이유)]
        self.ok_confirms = ["사업자단위과세"]     # 저장 때 [확인]을 눌러도 되는 안내 (글자 일부). BMS 설정에서 내려받아 바꿈
        self.confirmed = []

    # ── 화면 이동 ──
    def open_list(self):
        p = self.page
        if not p.locator(LIST).count():
            p.evaluate("(h) => { location.hash = h; }", MENU_HASH)
            try:
                p.wait_for_selector(LIST + ' button#new', timeout=15000)
            except Exception:
                p.get_by_text("관리", exact=True).first.click()
                p.wait_for_timeout(800)
                p.get_by_text("급여관리", exact=True).locator("visible=true").first.click()
                p.wait_for_timeout(600)
                links = p.locator("a:visible").filter(has_text="사원등록")
                next(links.nth(i) for i in range(links.count()) if links.nth(i).inner_text().strip() == "사원등록").click()
                p.wait_for_selector(LIST + ' button#new', timeout=20000)
        self.close_dialogs()

    def close_dialogs(self):
        """열려 있는 창(사원 창·검색 창)을 저장 없이 닫음."""
        p = self.page
        for _ in range(4):
            btns = p.locator('div.ui-dialog button#popupClose, div.ui-dialog button#outputClose, div.ui-dialog button#close')
            if not btns.count():
                return
            btns.last.evaluate("e => e.click()")
            p.wait_for_timeout(700)

    def find(self, emp_no: str) -> bool:
        """사원번호로 조회. 목록에 정확히 그 번호가 있으면 True."""
        p = self.page
        q = p.locator(LIST + ' input[data-cid="__headerQuick"]').locator("visible=true").first
        q.fill(emp_no)
        q.press("Enter")
        p.wait_for_timeout(2500)
        return p.locator(f'table#grid-main tr[data-key="{emp_no}"]').count() > 0

    def open_new(self):
        p = self.page
        p.locator(LIST + ' button#new').locator("visible=true").first.click()
        p.wait_for_selector(DLG + ' input[data-cid="EMP_CD"]', timeout=15000)
        p.wait_for_timeout(800)

    def open_existing(self, emp_no: str):
        p = self.page
        if not self.find(emp_no):
            raise FormError(f"이카운트 사원 목록에서 {emp_no} 을(를) 찾지 못했습니다")
        row = p.locator(f'table#grid-main tr[data-key="{emp_no}"]').first
        row.locator("a").first.click()
        p.wait_for_selector(DLG + ' input[data-cid="EMP_KNAME"]', timeout=15000)   # 저장된 사원은 사원번호가 입력칸이 아님
        p.wait_for_timeout(1200)

    @property
    def d(self):
        return self.page.locator(DLG).last

    def tab(self, tid: str):
        self.d.locator(f"li#{tid} a").locator("visible=true").first.click()
        self.page.wait_for_timeout(700)

    # ── 칸 넣기 ──
    def _inp(self, cid: str, idx: int = 0):
        return self.d.locator(f'input[data-cid="{cid}"][data-index="{idx}"], textarea[data-cid="{cid}"][data-index="{idx}"]') \
            .locator("visible=true").first

    def _skip(self, label, why):
        self.skipped.append((label, why))
        self.log(f"   · 못 넣음: {label} — {why}")

    def set_text(self, label, cid, value):
        el = self._inp(cid)
        el.click()
        el.fill("")
        el.type(str(value), delay=15)
        el.press("Tab")
        self.filled.append(label)

    def _search_popup_open(self) -> bool:
        return self.page.locator('div.ui-dialog:not([data-popup-id^="EPB001M"])').locator("visible=true").count() > 0

    def _close_search_popup(self):
        b = self.page.locator('div.ui-dialog:not([data-popup-id^="EPB001M"]) button#outputClose, '
                              'div.ui-dialog:not([data-popup-id^="EPB001M"]) button#popupClose')
        for i in range(b.count()):
            b.nth(i).evaluate("e => e.click()")
        self.page.wait_for_timeout(600)

    def set_code(self, label, cid, text) -> bool:
        """코드 칸(부서·직급·직책): 이름이나 코드를 치고 Enter → 하나로 정해지면 성공."""
        el = self._inp(cid)
        el.click()
        el.fill("")
        el.type(str(text), delay=30)
        el.press("Enter")
        self.page.wait_for_timeout(1500)
        if self._search_popup_open():
            self._close_search_popup()
            el.fill("")
            self._skip(label, f"이카운트에 '{text}'(이)가 없거나 여러 개입니다")
            return False
        name = self._inp(cid, 1).input_value() if self.d.locator(f'input[data-cid="{cid}"][data-index="1"]').count() else el.input_value()
        if not name:
            self._skip(label, f"'{text}' 을(를) 이카운트가 받아들이지 않았습니다")
            return False
        self.filled.append(label)
        return True

    def set_bank(self, text) -> bool:
        el = self._inp("BANKACCOUNT_bank")
        el.click()
        el.fill("")
        el.type(str(text).replace("은행", "") or str(text), delay=30)
        el.press("Enter")
        self.page.wait_for_timeout(1500)
        if self._search_popup_open():
            self._close_search_popup()
            el.fill("")
            self._skip("은행", f"이카운트 은행 목록에서 '{text}' 을(를) 하나로 정하지 못했습니다")
            return False
        if not el.input_value():
            self._skip("은행", f"'{text}' 을(를) 이카운트가 받아들이지 않았습니다")
            return False
        self.filled.append("은행")
        return True

    def _pick(self, cid, idx, value, text):
        """날짜의 연·월 고르기: 버튼을 눌러 뜨는 목록에서 값으로 고름."""
        p = self.page
        self.d.locator(f'button[data-cid="{cid}"][data-index="{idx}"]').locator("visible=true").first.click()
        p.wait_for_timeout(400)
        menu = p.locator("ul.dropdown-menu-selectbox").locator("visible=true").last
        item = menu.locator(f'li[data-value="{value}"] a')
        if not item.count():
            item = menu.locator("li a").filter(has_text=re.compile(rf"^\s*{re.escape(text)}\s*$"))
        if not item.count():
            direct = menu.locator('li[data-value="직접입력"] a')
            if not direct.count():
                p.locator("body").click(position={"x": 5, "y": 5})
                return False
            direct.first.click()                      # 목록에 없는 해(예: 생년)는 「직접입력」으로
            p.wait_for_timeout(300)
            box = self.d.locator(f'input.select-direct-input[data-cid="{cid}"][data-index="{idx}"]').locator("visible=true")
            if not box.count():
                return False
            box.first.fill("")
            box.first.type(str(value), delay=20)
            box.first.press("Tab")
            p.wait_for_timeout(300)
            return True
        item.first.click()
        p.wait_for_timeout(300)
        return True

    def set_date(self, label, cid, iso) -> bool:
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", str(iso or ""))
        if not m:
            self._skip(label, f"날짜 형식이 아닙니다: {iso}")
            return False
        y, mo, d = m.groups()
        if not self._pick(cid, 0, y, y):
            self._skip(label, f"연도 목록에 {y}년이 없습니다")
            return False
        if not (self._pick(cid, 1, mo, f"{int(mo)}월") or self._pick(cid, 1, str(int(mo)), f"{mo}월")):
            self._skip(label, f"월 목록에서 {int(mo)}월을 고르지 못했습니다")
            return False
        day = self._inp(cid, 2)
        day.click()
        day.fill("")
        day.type(d, delay=20)
        day.press("Tab")
        self.filled.append(label)
        return True

    def get_date(self, cid) -> str:
        def part(idx):
            b = self.d.locator(f'button[data-cid="{cid}"][data-index="{idx}"]').locator("visible=true")
            v = _digits(b.first.inner_text()) if b.count() else ""
            if not v:
                box = self.d.locator(f'input.select-direct-input[data-cid="{cid}"][data-index="{idx}"]').locator("visible=true")
                v = _digits(box.first.input_value()) if box.count() else ""
            return v
        y, mo = part(0), part(1)
        d = _digits(self._inp(cid, 2).input_value())
        return f"{y}-{int(mo):02d}-{int(d):02d}" if y and mo and d else ""

    def set_radio(self, label, cid, value):
        self.d.locator(f'input[type=radio][data-cid="{cid}"][value="{value}"]').locator("visible=true").first.check()
        self.filled.append(label)

    def get_radio(self, cid) -> str:
        r = self.d.locator(f'input[type=radio][data-cid="{cid}"]:checked')
        return r.first.get_attribute("value") if r.count() else ""

    def pay_rows(self) -> dict:
        """급여지급사항 표: {항목 이름(공백 없이): 내역 칸}  — 같은 이름이 수당·공제 양쪽에 있으면 위쪽(수당)."""
        keys = self.d.evaluate("""d => [...d.querySelectorAll('td[data-columnid="ALLODEDU_NAME"]')].filter(t => t.offsetWidth)
            .map(t => [[...t.innerText].filter(c => c.trim()).join(''), t.getAttribute('data-key')])""")
        out = {}
        for name, key in keys:
            if name and name not in out:
                out[name] = self.d.locator(f'td[data-columnid="ALLODEDU_VALUE"][data-key="{key}"]').locator("visible=true").first
        return out

    def set_pay(self, items: dict):
        rows = self.pay_rows()
        for name, amount in items.items():
            if not amount:
                continue
            r = rows.get(_n(name))
            if not r:
                self._skip(f"급여항목 {name}", "이카운트 급여지급사항 표에 같은 이름이 없습니다")
                continue
            r.click()
            self.page.wait_for_timeout(250)
            self.page.keyboard.press("Control+A")
            self.page.keyboard.type(str(int(amount)))
            self.page.keyboard.press("Tab")
            self.page.wait_for_timeout(200)
            self.filled.append(f"급여항목 {name}")

    def list_depts(self) -> list:
        """이카운트 부서 목록 [[코드, 이름], ...] — 새 사원 창의 부서 검색 창에서 읽음 (저장 없음)."""
        p = self.page
        self.tab("A1")
        self.d.locator('button[data-cid="EMP_SITE"]').locator("visible=true").first.click()
        pop = p.locator('div.ui-dialog[data-popup-id^="EBA003P"]')
        pop.last.wait_for(state="visible", timeout=10000)
        p.wait_for_timeout(1500)
        rows = pop.last.evaluate("d => [...d.querySelectorAll('table tr')].filter(t => t.offsetWidth && t.querySelector('td'))"
                                 ".map(t => [...t.querySelectorAll('td')].map(c => c.innerText.trim()).filter(Boolean))")
        self._close_search_popup()
        return [r[:2] for r in rows if len(r) >= 2]

    # ── 한 사람 채우기 ──
    def fill_new(self, v: dict):
        """v: BMS 가 준 값. 빈 값은 넣지 않음."""
        self.tab("A1")
        for key, label, _tab, cid in TEXT_FIELDS:
            if v.get(key):
                self.set_text(label, cid, v[key])
        if v.get("hire_date"):
            self.set_date("입사일자", "IN_DATE", v["hire_date"])
        for key, label, cid in CODE_FIELDS:
            if v.get(key):
                self.set_code(label, cid, v[key])
        if v.get("dept_code"):
            self.set_code("부서", "EMP_SITE", v["dept_code"])
        elif v.get("department"):
            self.set_code("부서", "EMP_SITE", v["department"])
        if v.get("bank_name"):
            self.set_bank(v["bank_name"])
        if v.get("birth_date"):
            self.tab("A2")
            self.set_date("생년월일", "BIRTHDAY", v["birth_date"])
        if v.get("pay_std_code") or v.get("pay_items"):
            self.tab("A3")
            if v.get("pay_std_code") and self.set_code("지급구분", "PAY_STD_TPCD", v["pay_std_code"]):
                self.page.wait_for_timeout(1500)
                self.set_pay(v.get("pay_items") or {})
            elif v.get("pay_items"):
                self._skip("급여항목", "지급구분이 정해지지 않아 급여지급사항 표가 열리지 않았습니다")
        if v.get("annual_salary"):
            self.tab("A4")
            if self.d.locator('input[data-cid="ADD_NUM_01"]').locator("visible=true").count():
                self.set_text("연봉(추가정보)", "ADD_NUM_01", str(int(v["annual_salary"])))
        self.tab("A5")
        for key, label, cid in INSURANCE:
            if v.get(key):
                self.set_radio(label + " 기준", cid + "_typeRadio", "1")
                self.page.wait_for_timeout(300)
                self.set_text(label, cid + "_input_1", str(int(v[key])))
        if v.get("householder") in ("Y", "N"):
            self.set_radio("세대주여부", "HOLDER_TYPE", "1" if v["householder"] == "Y" else "2")
        if v.get("dependents") not in (None, ""):
            self.set_text("부양가족수", "FAMILY_CNT_input_1", str(v["dependents"]))
        if v.get("children_8_20") not in (None, ""):
            self.set_text("8~20세 자녀수", "FAMILY_CNT_input_2", str(v["children_8_20"]))
        self.tab("A1")

    def fill_resign(self, v: dict):
        self.tab("A2")
        self.set_date("퇴사일자", "OUT_DATE", v["resign_date"])
        if v.get("resign_reason"):
            self.set_text("퇴사사유", "OUT_DES", v["resign_reason"])

    # ── 저장·대조 ──
    def save(self) -> str:
        """저장을 누르고, 창이 닫히면 '' / 안 닫히면 화면에 뜬 안내 글을 돌려줌."""
        p = self.page
        msgs = []
        handler = lambda dlg: (msgs.append(dlg.message), dlg.accept())    # noqa: E731
        p.on("dialog", handler)
        try:
            self.d.locator("button#save").locator("visible=true").first.click()
            for _ in range(24):
                p.wait_for_timeout(500)
                if not p.locator(DLG).locator("visible=true").count():
                    return ""
                ask = p.locator('div.ui-dialog[data-popup-id^="confirm"]').locator("visible=true")
                if ask.count():                       # 이카운트가 되묻는 창: 미리 허락된 안내만 [확인], 나머지는 [취소]하고 멈춤
                    text = " ".join(ask.last.inner_text().split())
                    if any(k in text for k in self.ok_confirms):
                        self.confirmed.append(text[:120])
                        ask.last.locator("button#btn_confirm_ok").click()
                        continue
                    ask.last.locator("button#btn_confirm_cancel").click()
                    return "이카운트가 되물었습니다: " + text[:200]
        finally:
            p.remove_listener("dialog", handler)
        note = p.evaluate("""() => [...document.querySelectorAll('.has-error, .tooltip, .popover, .alert, .toast, .validation, [class*=error]')]
            .filter(e => e.offsetWidth && e.innerText.trim()).map(e => e.innerText.trim().slice(0, 80)).slice(0, 6).join(' / ')""")
        other = p.evaluate("""() => [...document.querySelectorAll('div.ui-dialog')].filter(d => d.offsetWidth && !(d.getAttribute('data-popup-id') || '').startsWith('EPB001M'))
            .map(d => d.innerText.trim().slice(0, 200)).join(' / ')""")
        return " / ".join(x for x in (" ".join(msgs), note, other) if x) or "저장을 눌렀지만 창이 닫히지 않았습니다"

    def read_back(self) -> dict:
        """열린 사원 창의 값을 읽음 (대조용)."""
        out = {}

        def grab(cid, idx=0):
            """지금 탭에 그 칸이 보이면 값, 없으면 None (저장된 사원 창은 은행·계좌가 기타설정 탭에만 있음)."""
            el = self.d.locator(f'input[data-cid="{cid}"][data-index="{idx}"], textarea[data-cid="{cid}"][data-index="{idx}"]').locator("visible=true")
            return el.first.input_value() if el.count() else None
        late = []
        self.tab("A1")
        for key, _label, _tab, cid in TEXT_FIELDS:
            if key == "emp_no":
                continue
            val = grab(cid)
            if val is None:
                late.append((key, cid))
            else:
                out[key] = val
        out["hire_date"] = self.get_date("IN_DATE")
        for key, _label, cid in CODE_FIELDS:
            out[key] = self._inp(cid, 1).input_value()
        out["dept_code"] = self._inp("EMP_SITE", 0).input_value()
        out["department"] = self._inp("EMP_SITE", 1).input_value()
        bank = grab("BANKACCOUNT_bank")
        self.tab("A2")
        out["birth_date"] = self.get_date("BIRTHDAY")
        out["resign_date"] = self.get_date("OUT_DATE")
        self.tab("A3")
        out["pay_std_code"] = self._inp("PAY_STD_TPCD", 0).input_value() if self.d.locator('input[data-cid="PAY_STD_TPCD"]').locator("visible=true").count() else ""
        self.page.wait_for_timeout(600)
        out["pay_items"] = {}
        for name, cell in self.pay_rows().items():
            val = _digits(cell.inner_text())
            if val:
                out["pay_items"][name] = int(val)
        self.tab("A4")
        if self.d.locator('input[data-cid="ADD_NUM_01"]').locator("visible=true").count():
            out["annual_salary"] = _digits(self._inp("ADD_NUM_01").input_value().split(".")[0])
        self.tab("A5")
        for key, cid in late:
            out[key] = grab(cid) or ""
        out["bank_name"] = bank if bank is not None else (grab("BANKACCOUNT_bank") or "")
        link = self.d.locator("a#BANKACCOUNT").locator("visible=true")
        if link.count():                                  # 저장된 사원: 급여통장은 [변경] 창 안에 있음 (읽기만 하고 닫음)
            link.first.click()
            pop = self.page.locator('div.ui-dialog[data-popup-id^="ISalaryBankAccountPopupPage"]')
            try:
                pop.last.wait_for(state="visible", timeout=8000)
                vals = []
                for _ in range(12):                       # 값이 채워질 때까지 잠깐 기다림
                    self.page.wait_for_timeout(500)
                    vals = pop.last.evaluate("d => [...d.querySelectorAll('input')].filter(b => b.type === 'text' && b.offsetWidth).map(b => b.value)")
                    if len(vals) >= 4 and any(vals):
                        break
                if len(vals) >= 4:
                    out["bank_name"], out["bank_account"], out["account_holder"] = vals[1], vals[2], vals[3]
                pop.last.evaluate("d => { const b = [...d.querySelectorAll('button')].filter(x => x.innerText.trim() === '닫기').pop(); if (b) b.click(); }")
                self.page.wait_for_timeout(600)
            except Exception:                             # noqa: BLE001 — 못 읽으면 대조에서 '다름'으로 드러남
                pass
        for key, _label, cid in INSURANCE:
            on = self.get_radio(cid + "_typeRadio") == "1"
            out[key] = _digits(self._inp(cid + "_input_1").input_value().split(".")[0]) if on else ""
        out["householder"] = {"1": "Y", "2": "N", "3": "N"}.get(self.get_radio("HOLDER_TYPE"), "")
        out["dependents"] = _digits(self._inp("FAMILY_CNT_input_1").input_value())
        out["children_8_20"] = _digits(self._inp("FAMILY_CNT_input_2").input_value())
        self.tab("A1")
        return out


LABELS = {"emp_no": "사원번호", "name": "성명", "rrn": "주민등록번호", "email": "이메일", "phone": "휴대폰", "bank_account": "계좌번호",
          "account_holder": "예금주", "address": "주소", "hire_date": "입사일자", "birth_date": "생년월일", "rank": "직급", "title": "직책",
          "dept_code": "부서", "bank_name": "은행", "pay_std_code": "지급구분", "annual_salary": "연봉(추가정보)",
          "insured_monthly": "건강보험 보수월액", "pension_monthly": "국민연금 기준소득월액", "employ_monthly": "고용보험 보수월액",
          "householder": "세대주여부", "dependents": "부양가족수", "children_8_20": "8~20세 자녀수", "resign_date": "퇴사일자"}
_NUMERIC = {"rrn", "phone", "bank_account", "annual_salary", "insured_monthly", "pension_monthly", "employ_monthly", "dependents", "children_8_20"}


def compare(want: dict, got: dict, keys=None) -> list[str]:
    """BMS 값과 이카운트에 저장된 값이 다른 칸의 '이름' 목록 (값은 담지 않음)."""
    bad = []
    for k in (keys or LABELS):
        if k == "emp_no":                # 사원번호로 찾아 연 창이므로 따로 대조하지 않음
            continue
        w = want.get(k)
        if w in (None, ""):
            continue
        g = got.get(k, "")
        if k in _NUMERIC:
            a, b = _digits(str(w).split(".")[0]), _digits(g)
            ok = a == b or (k == "rrn" and len(b) >= 6 and a[:6] == b[:6] and (len(b) < 13 or "*" in str(g)))   # 화면이 뒷자리를 가리면 앞자리만
            ok = ok or (a.lstrip("0") == b.lstrip("0"))
        elif k == "bank_name":
            ok = _n(w).replace("은행", "") in _n(g)
        elif k in ("rank", "title"):
            ok = _n(w) == _n(g)
        else:
            ok = _n(w) == _n(g)
        if not ok:
            bad.append(LABELS[k])
    for name, amount in (want.get("pay_items") or {}).items():
        if amount and int(amount) != int((got.get("pay_items") or {}).get(_n(name), 0)):
            bad.append(f"급여항목 {name}")
    return bad
