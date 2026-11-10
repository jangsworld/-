"""총무관리 데이터 접근 계층."""
import re

from app.common import today, to_date
from app.db import query, query_one, execute, audit, get_behaviors
from app.security.crypto import enc
from app.modules.ga.constants import ASSET_BEHAVIORS, ASSET_CATEGORIES, DEFAULT_EVENT_RULES, ODOMETER_SUSPECT_KM

DRIVER_TAG = re.compile(r"\s*\[카택스 운전자: (.+?)\]")   # 임직원과 연결되지 않은 카택스 운전자 이름

# 테이블별 저장 가능 필드 / 정수 필드 / 암호화 필드
TABLES = {
    "ga_assets": (["asset_no", "category", "name", "model", "serial_no", "vendor", "acquired_date", "cost",
                   "useful_years", "location", "status", "holder_id", "warranty_end", "disposed_date", "memo", "cost_vat", "photo"],
                  {"cost", "useful_years", "holder_id"}, set()),
    "ga_asset_history": (["event_date", "event_type", "holder_id", "cost", "cost_vat", "note"], {"holder_id", "cost"}, set()),
    "ga_vehicles": (["plate_no", "model", "model_year", "fuel", "ownership", "acquired_date", "lease_end",
                     "monthly_cost", "insurer", "insurance_end", "inspection_due", "manager_id", "status",
                     "odometer_start", "memo"],
                    {"model_year", "monthly_cost", "manager_id", "odometer_start"}, set()),
    "ga_vehicle_logs": (["vehicle_id", "drive_date", "driver_id", "purpose", "destination", "km_start", "km_end",
                         "fuel_cost", "toll_cost", "note", "external_id", "external_updated", "external_purpose"],
                        {"vehicle_id", "driver_id", "km_start", "km_end", "fuel_cost", "toll_cost"}, {"destination"}),
    "ga_vehicle_maint": (["vehicle_id", "maint_date", "category", "detail", "km", "cost", "cost_vat", "vendor"],
                         {"vehicle_id", "km", "cost"}, set()),
    "ga_docs": (["direction", "doc_no", "doc_date", "counterpart", "title", "department", "handler_id", "method",
                 "status", "due_date", "retention", "note"], {"handler_id"}, set()),
    "ga_seal_logs": (["use_date", "seal_type", "purpose", "submit_to", "copies", "requester_id", "status",
                      "approver", "approved_at", "note"], {"copies", "requester_id"}, set()),
    "ga_contracts": (["category", "title", "counterparty", "contract_no", "start_date", "end_date", "amount", "amount_vat",
                      "pay_cycle", "deposit", "auto_renew", "notice_days", "manager_id", "status", "memo", "folder_name"],
                     {"amount", "deposit", "auto_renew", "notice_days", "manager_id"}, set()),
    "ga_events": (["employee_id", "category", "relation", "event_date", "amount", "leave_days", "flower",
                   "paid_date", "status", "detail"],
                  {"employee_id", "amount", "leave_days", "flower"}, {"detail"}),
    "ga_event_rules": (["category", "relation", "amount", "leave_days", "flower"], {"amount", "leave_days", "flower"}, set()),
}
LABELS = {"ga_assets": "자산", "ga_asset_history": "자산이력", "ga_vehicles": "차량", "ga_vehicle_logs": "운행일지", "ga_vehicle_maint": "차량정비",
          "ga_docs": "문서수발", "ga_seal_logs": "관인사용", "ga_contracts": "계약", "ga_events": "경조사",
          "ga_event_rules": "경조규정"}


def _int(v):
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    v = str(v).replace(",", "").strip()
    if v.lower() in ("on", "true"):
        return 1
    return int(float(v)) if v else None


def save(table: str, rid: int | None, data: dict, quiet: bool = False) -> int:
    fields, ints, encrypted = TABLES[table]
    d = {}
    for f in fields:
        if f not in data:
            continue
        v = data[f]
        v = v.strip() if isinstance(v, str) else v
        v = None if v == "" else v
        if f in ints:
            v = _int(v)
        if f in encrypted:
            v = enc(v)
        d[f] = v
    if rid:
        sets = ", ".join(f"{k}=?" for k in d)
        execute(f"UPDATE {table} SET {sets} WHERE id=?", list(d.values()) + [rid])
        if not quiet:
            audit("ga", f"{LABELS[table]} 수정", f"#{rid}", "")
        return rid
    new_id = execute(f"INSERT INTO {table} ({', '.join(d)}) VALUES ({', '.join('?' * len(d))})", list(d.values()))
    if not quiet:
        audit("ga", f"{LABELS[table]} 등록", f"#{new_id}", "")
    return new_id


def delete(table: str, rid: int):
    if table not in TABLES:
        raise ValueError(table)
    execute(f"DELETE FROM {table} WHERE id=?", (rid,))
    audit("ga", f"{LABELS[table]} 삭제", f"#{rid}", "")


def get(table: str, rid: int) -> dict | None:
    if table not in TABLES:
        raise ValueError(table)
    return query_one(f"SELECT * FROM {table} WHERE id=?", (rid,))


def employees_brief(active_only=True) -> list[dict]:
    sql = "SELECT id, emp_no, name, department, rank, resign_date FROM employees"
    if active_only:
        sql += " WHERE resign_date IS NULL OR resign_date='' OR resign_date > date('now','localtime')"
    return query(sql + " ORDER BY department, name")


# ── 자산 ────────────────────────────────────────────────────────────────
def next_asset_no(category: str, acquired: str | None) -> str:
    prefix = ASSET_CATEGORIES.get(category, ("ET", 5))[0]
    year = (to_date(acquired) or today()).year
    row = query_one("SELECT asset_no FROM ga_assets WHERE asset_no LIKE ? ORDER BY asset_no DESC LIMIT 1",
                    (f"{prefix}-{year}-%",))
    seq = int(row["asset_no"].rsplit("-", 1)[1]) + 1 if row else 1
    return f"{prefix}-{year}-{seq:03d}"


def list_assets(category="", status="", holder_id=None, q="") -> list[dict]:
    sql = ("SELECT a.*, e.name holder_name, e.department holder_dept, e.resign_date holder_resign "
           "FROM ga_assets a LEFT JOIN employees e ON e.id=a.holder_id WHERE 1=1")
    p = []
    if category:
        sql += " AND a.category=?"; p.append(category)
    if status:
        sql += " AND a.status=?"; p.append(status)
    if holder_id:
        sql += " AND a.holder_id=?"; p.append(holder_id)
    if q:
        sql += " AND (a.asset_no LIKE ? OR a.name LIKE ? OR a.model LIKE ? OR a.serial_no LIKE ? OR e.name LIKE ?)"
        p += [f"%{q}%"] * 5
    return query(sql + " ORDER BY a.asset_no", p)


def asset_qr_token(asset_id: int) -> str:
    """자산 라벨 QR 에 넣는 무작위 열쇠 (자산번호만으로는 다른 자산을 짐작해 열 수 없게)."""
    import secrets
    row = query_one("SELECT qr_token FROM ga_assets WHERE id=?", (asset_id,))
    if row and row["qr_token"]:
        return row["qr_token"]
    token = secrets.token_urlsafe(12)
    execute("UPDATE ga_assets SET qr_token=? WHERE id=? AND (qr_token IS NULL OR qr_token='')", (token, asset_id))
    return query_one("SELECT qr_token FROM ga_assets WHERE id=?", (asset_id,))["qr_token"]


def ensure_qr_tokens():
    for r in query("SELECT id FROM ga_assets WHERE qr_token IS NULL OR qr_token=''"):
        asset_qr_token(r["id"])


def asset_by_qr(token: str) -> dict | None:
    """QR 공개 화면용: 기본 정보만 (금액·시리얼·이력 제외)."""
    if not token or len(token) > 40:
        return None
    return query_one("SELECT a.id, a.asset_no, a.category, a.name, a.model, a.status, a.location, a.acquired_date, "
                     "a.warranty_end, e.name holder_name, e.department holder_dept FROM ga_assets a "
                     "LEFT JOIN employees e ON e.id=a.holder_id WHERE a.qr_token=?", (token,))


def asset_history(asset_id: int) -> list[dict]:
    return query("SELECT h.*, e.name holder_name FROM ga_asset_history h LEFT JOIN employees e ON e.id=h.holder_id "
                 "WHERE h.asset_id=? ORDER BY h.event_date DESC, h.id DESC", (asset_id,))


def event_behavior(event_type: str) -> str:
    """처리 구분 → 상태 변경 방식. 설정에서 지운 구분의 과거 이력은 이름으로 판단."""
    behaviors = get_behaviors("ASSET_EVENT")
    if event_type in behaviors:
        return behaviors[event_type]
    return event_type if event_type in ASSET_BEHAVIORS else ""


def _apply_event(asset_id, behavior, holder_id, event_date):
    """이력에 따라 자산 상태·사용자 자동 반영."""
    if behavior == "지급":
        execute("UPDATE ga_assets SET holder_id=?, status='사용중' WHERE id=?", (holder_id, asset_id))
    elif behavior == "반납":
        execute("UPDATE ga_assets SET holder_id=NULL, status='보관' WHERE id=?", (asset_id,))
    elif behavior == "수리":
        execute("UPDATE ga_assets SET status='수리중' WHERE id=?", (asset_id,))
    elif behavior == "수리완료":
        execute("UPDATE ga_assets SET status=CASE WHEN holder_id IS NULL THEN '보관' ELSE '사용중' END WHERE id=?", (asset_id,))
    elif behavior == "폐기":
        execute("UPDATE ga_assets SET status='폐기', holder_id=NULL, disposed_date=? WHERE id=?", (event_date, asset_id))


def add_asset_event(asset_id, event_date, event_type, holder_id=None, cost=0, note="", cost_vat=None):
    execute("INSERT INTO ga_asset_history(asset_id, event_date, event_type, holder_id, cost, note, cost_vat) "
            "VALUES (?,?,?,?,?,?,?)", (asset_id, event_date, event_type, holder_id, _int(cost) or 0, note, cost_vat or None))
    _apply_event(asset_id, event_behavior(event_type), holder_id, event_date)
    audit("ga", f"자산 {event_type}", f"asset#{asset_id}", note or "")


def rebuild_asset_state(asset_id: int):
    """이력을 수정·삭제한 뒤 남은 이력을 시간순으로 다시 적용해 상태·사용자를 맞춥니다."""
    rows = query("SELECT * FROM ga_asset_history WHERE asset_id=? ORDER BY event_date, id", (asset_id,))
    execute("UPDATE ga_assets SET holder_id=NULL, status='보관', disposed_date=NULL WHERE id=?", (asset_id,))
    for r in rows:
        _apply_event(asset_id, event_behavior(r["event_type"]), r["holder_id"], r["event_date"])


# ── 차량 ────────────────────────────────────────────────────────────────
def list_vehicles() -> list[dict]:
    return query("SELECT v.*, e.name manager_name FROM ga_vehicles v LEFT JOIN employees e ON e.id=v.manager_id "
                 "ORDER BY v.status, v.plate_no")


def vehicle_logs(vehicle_id=None, year=None, month=None) -> list[dict]:
    sql = ("SELECT l.*, e.name driver_name, e.department driver_dept, v.plate_no FROM ga_vehicle_logs l "
           "JOIN ga_vehicles v ON v.id=l.vehicle_id LEFT JOIN employees e ON e.id=l.driver_id WHERE 1=1")
    p = []
    if vehicle_id:
        sql += " AND l.vehicle_id=?"; p.append(vehicle_id)
    if year:
        sql += " AND substr(l.drive_date,1,4)=?"; p.append(str(year))
    if month:
        sql += " AND substr(l.drive_date,1,7)=?"; p.append(month)
    rows = query(sql + " ORDER BY l.drive_date DESC, l.km_end DESC", p)
    for r in rows:
        r["distance"] = max(0, (r["km_end"] or 0) - (r["km_start"] or 0))
        r["suspect"] = r["distance"] > ODOMETER_SUSPECT_KM
        tag = DRIVER_TAG.search(r["note"] or "")
        if tag and not r["driver_name"]:
            r["driver_name"] = tag.group(1)
    return rows


def is_cartax_vehicle(vehicle_id: int) -> bool:
    return bool(query_one("SELECT 1 x FROM ga_vehicle_logs WHERE vehicle_id=? AND external_id IS NOT NULL LIMIT 1", (vehicle_id,)))


def is_cartax_log(log_id: int) -> bool:
    return bool(query_one("SELECT 1 x FROM ga_vehicle_logs WHERE id=? AND external_id IS NOT NULL", (log_id,)))


def last_odometer(vehicle_id: int) -> int:
    r = query_one("SELECT MAX(km_end) m FROM ga_vehicle_logs WHERE vehicle_id=?", (vehicle_id,))
    v = query_one("SELECT odometer_start FROM ga_vehicles WHERE id=?", (vehicle_id,))
    return max((r and r["m"]) or 0, (v and v["odometer_start"]) or 0)


def vehicle_maint(vehicle_id=None, year=None) -> list[dict]:
    sql = "SELECT m.*, v.plate_no FROM ga_vehicle_maint m JOIN ga_vehicles v ON v.id=m.vehicle_id WHERE 1=1"
    p = []
    if vehicle_id:
        sql += " AND m.vehicle_id=?"; p.append(vehicle_id)
    if year:
        sql += " AND substr(m.maint_date,1,4)=?"; p.append(str(year))
    return query(sql + " ORDER BY m.maint_date DESC", p)


# ── 문서수발 / 관인 ─────────────────────────────────────────────────────
def next_doc_no(direction: str, doc_date: str | None, prefix: str) -> str:
    year = (to_date(doc_date) or today()).year
    head = f"{prefix}-{year}-" if direction == "발신" else f"수신-{year}-"
    row = query_one("SELECT doc_no FROM ga_docs WHERE doc_no LIKE ? ORDER BY doc_no DESC LIMIT 1", (head + "%",))
    seq = int(row["doc_no"].rsplit("-", 1)[1]) + 1 if row else 1
    return f"{head}{seq:04d}"


def list_docs(direction="", year=None, q="", status="") -> list[dict]:
    sql = "SELECT d.*, e.name handler_name FROM ga_docs d LEFT JOIN employees e ON e.id=d.handler_id WHERE 1=1"
    p = []
    if direction:
        sql += " AND d.direction=?"; p.append(direction)
    if year:
        sql += " AND substr(d.doc_date,1,4)=?"; p.append(str(year))
    if status:
        sql += " AND d.status=?"; p.append(status)
    if q:
        sql += " AND (d.doc_no LIKE ? OR d.title LIKE ? OR d.counterpart LIKE ?)"; p += [f"%{q}%"] * 3
    return query(sql + " ORDER BY d.doc_date DESC, d.doc_no DESC", p)


def list_seals(year=None, status="") -> list[dict]:
    sql = ("SELECT s.*, e.name requester_name, e.department requester_dept FROM ga_seal_logs s "
           "LEFT JOIN employees e ON e.id=s.requester_id WHERE 1=1")
    p = []
    if year:
        sql += " AND substr(s.use_date,1,4)=?"; p.append(str(year))
    if status:
        sql += " AND s.status=?"; p.append(status)
    return query(sql + " ORDER BY s.use_date DESC, s.id DESC", p)


# ── 계약 ────────────────────────────────────────────────────────────────
def list_contracts(category="", status="") -> list[dict]:
    sql = "SELECT c.*, e.name manager_name FROM ga_contracts c LEFT JOIN employees e ON e.id=c.manager_id WHERE 1=1"
    p = []
    if category:
        sql += " AND c.category=?"; p.append(category)
    if status:
        sql += " AND c.status=?"; p.append(status)
    return query(sql + " ORDER BY CASE c.status WHEN '유효' THEN 0 ELSE 1 END, c.end_date", p)


# ── 경조사 ─────────────────────────────────────────────────────────────
def ensure_event_rules():
    if query_one("SELECT COUNT(*) c FROM ga_event_rules")["c"]:
        return
    for cat, rel, amt, days, flower in DEFAULT_EVENT_RULES:
        execute("INSERT INTO ga_event_rules(category, relation, amount, leave_days, flower) VALUES (?,?,?,?,?)",
                (cat, rel, amt, days, flower))


def event_rules() -> list[dict]:
    return query("SELECT * FROM ga_event_rules ORDER BY category, amount DESC")


def list_events(year=None, status="", employee_id=None) -> list[dict]:
    sql = ("SELECT v.*, e.name, e.emp_no, e.department, e.rank FROM ga_events v "
           "JOIN employees e ON e.id=v.employee_id WHERE 1=1")
    p = []
    if year:
        sql += " AND substr(v.event_date,1,4)=?"; p.append(str(year))
    if status:
        sql += " AND v.status=?"; p.append(status)
    if employee_id:
        sql += " AND v.employee_id=?"; p.append(employee_id)
    return query(sql + " ORDER BY v.event_date DESC, v.id DESC", p)
