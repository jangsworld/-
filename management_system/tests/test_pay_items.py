"""급여항목 구성(2026-11)과 예전 이름(연장근로수당 등) 옮기기."""
import json
import sqlite3

from app.db import _migrate_pay_items
from app.modules.hr import ecount


def test_default_pay_items():
    names = ecount.PAY_ITEMS_DEFAULT.split(",")
    assert names[:3] == ["기본급", "시간외수당(월24시간)", "야간수당(월8시간)"]
    assert names[-1] == "퇴직위로금" and len(names) == 20 and len(set(names)) == 20


def test_aliases_find_ecount_columns(monkeypatch):
    monkeypatch.setattr(ecount, "get_settings", lambda: {"ecount_pay_items": ecount.PAY_ITEMS_DEFAULT})
    t = {name: al for _k, name, al in ecount.all_targets()}
    assert "연장근로수당" in t["시간외수당(월24시간)"] and "야간수당" in t["야간수당(월8시간)"]
    assert "자가운전보조금" in t["자가운전보조금(비과세)"]


def test_migrate_renames_saved_amounts():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT)")
    con.execute("CREATE TABLE employee_payroll(employee_id INTEGER, pay_items TEXT)")
    con.execute("INSERT INTO settings VALUES ('ecount_pay_items','기본급,연장근로수당')")
    con.execute("INSERT INTO settings VALUES ('ecount_template', ?)",
                (json.dumps({"columns": [{"field": "pay:연장근로수당"}]}, ensure_ascii=False),))
    con.execute("INSERT INTO employee_payroll VALUES (1, ?)",
                (json.dumps({"기본급": 100, "연장근로수당": 20, "자가운전보조금": 5}, ensure_ascii=False),))
    _migrate_pay_items(con)
    _migrate_pay_items(con)          # 두 번 돌려도 그대로
    items = json.loads(con.execute("SELECT pay_items FROM employee_payroll").fetchone()[0])
    assert items == {"기본급": 100, "시간외수당(월24시간)": 20, "자가운전보조금(비과세)": 5}
    s = dict(con.execute("SELECT key, value FROM settings").fetchall())
    assert s["ecount_pay_items"] == ecount.PAY_ITEMS_DEFAULT and "pay:시간외수당(월24시간)" in s["ecount_template"]
