"""법인차량 관리 강화: 사용자 이력, 인수인계 사진 점검·비교, 사고·파손, 과태료, 비용 분석, 차량 QR.

- 사진 점검은 확정되면 고칠 수 없습니다 (사진 추가·삭제·판정 변경 불가). 사진마다 저장 파일 지문(sha256)을 남깁니다.
- 새 파손의 책임 후보는 '앞 점검 ~ 이번 점검' 사이에 차를 쓴 사람(사용자 이력 + 운행기록)입니다.
"""
import hashlib
import secrets
from collections import defaultdict
from datetime import datetime, timedelta

from app.common import add_months, days_until, today, to_date, vat_split
from app.db import audit, execute, query, query_one
from app.modules.ga import photos, repo

SLOTS = [("front", "앞"), ("rear", "뒤"), ("left", "왼쪽 옆"), ("right", "오른쪽 옆"),
         ("driver", "운전석"), ("rear_seat", "뒷좌석"), ("dash", "계기판(주행거리·연료)"), ("trunk", "트렁크")]
SLOT_LABEL = dict(SLOTS) | {"damage": "파손 부위"}
KINDS = ["신차인수", "사용자변경", "반납", "정기점검", "수시점검"]
SELF_KINDS = ["정기점검", "수시점검"]           # 직원이 차량 QR 로 직접 시작할 수 있는 점검
REPORT_KIND = "파손신고"
VERDICTS = ["이상없음", "새파손", "기존파손"]
DAMAGE_KINDS = ["사고", "파손", "고장"]
DAMAGE_STATUSES = ["접수", "수리중", "수리완료", "종결"]
INSURANCE = ["미정", "보험처리", "회사부담", "본인부담"]
RESPONSIBILITY = ["미정", "운전자", "회사", "상대방"]
FINE_KINDS = ["속도위반", "신호위반", "주정차위반", "버스전용차로", "통행료 미납", "기타"]
FINE_STATUSES = ["접수", "운전자확인", "납부완료", "급여공제", "이의신청", "종결"]
FINE_OPEN = ("접수", "운전자확인", "이의신청")
OIL_DEFAULT_KM = 10000
SESSION_MINUTES = 30
MAX_FAILS = 10
_fails: dict[int, tuple[int, datetime]] = {}    # 차량별 본인확인 실패 (횟수, 잠금 해제 시각)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _int(v, default=0) -> int:
    try:
        return int(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def emp_name(emp_id) -> str:
    r = query_one("SELECT name FROM employees WHERE id=?", (emp_id,)) if emp_id else None
    return r["name"] if r else ""


# ── 차량 QR ─────────────────────────────────────────────────────────────
def qr_token(vehicle_id: int) -> str:
    row = query_one("SELECT qr_token FROM ga_vehicles WHERE id=?", (vehicle_id,))
    if row and row["qr_token"]:
        return row["qr_token"]
    execute("UPDATE ga_vehicles SET qr_token=? WHERE id=? AND (qr_token IS NULL OR qr_token='')",
            (secrets.token_urlsafe(12), vehicle_id))
    return query_one("SELECT qr_token FROM ga_vehicles WHERE id=?", (vehicle_id,))["qr_token"]


def vehicle_by_qr(token: str) -> dict | None:
    if not token or len(token) > 40:
        return None
    return query_one("SELECT * FROM ga_vehicles WHERE qr_token=? AND status!='매각'", (token,))


def verify_person(vehicle_id: int, name: str, birth6: str) -> tuple[dict | None, str]:
    """차량 QR 본인 확인: 재직 중인 직원의 이름 + 생년월일 6자리."""
    cnt, until = _fails.get(vehicle_id, (0, datetime.min))
    if cnt >= MAX_FAILS and until > datetime.now():
        return None, "본인 확인을 여러 번 실패해 15분 동안 잠겼습니다. 관리부에 문의해 주세요."
    name = (name or "").strip()
    birth6 = "".join(ch for ch in str(birth6 or "") if ch.isdigit())
    t = today().isoformat()
    for e in query("SELECT id, name, birth_date, resign_date FROM employees WHERE name=?", (name,)):
        if e["resign_date"] and e["resign_date"] < t:
            continue
        expected = (e["birth_date"] or "").replace("-", "")[2:]
        if expected and len(birth6) == 6 and secrets.compare_digest(expected.encode(), birth6.encode()):
            _fails.pop(vehicle_id, None)
            return e, ""
    cnt = cnt + 1 if until > datetime.now() else 1
    _fails[vehicle_id] = (cnt, datetime.now() + timedelta(minutes=15))
    audit("ga", "차량 QR 본인확인 실패", f"vehicle#{vehicle_id}", name[:10])
    return None, "이름 또는 생년월일이 맞지 않습니다."


def open_session(vehicle_id: int, employee_id: int) -> str:
    s = secrets.token_urlsafe(32)
    exp = (datetime.now() + timedelta(minutes=SESSION_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
    execute("DELETE FROM ga_vehicle_access WHERE expires < ?", (_now(),))
    execute("INSERT INTO ga_vehicle_access(vehicle_id, employee_id, session_hash, expires) VALUES (?,?,?,?)",
            (vehicle_id, employee_id, _hash(s), exp))
    audit("ga", "차량 QR 본인확인", f"vehicle#{vehicle_id}", emp_name(employee_id))
    return s


def session_employee(vehicle_id: int, cookie: str) -> dict | None:
    if not cookie:
        return None
    r = query_one("SELECT employee_id FROM ga_vehicle_access WHERE session_hash=? AND vehicle_id=? AND expires>?",
                  (_hash(cookie), vehicle_id, _now()))
    return query_one("SELECT id, name, department FROM employees WHERE id=?", (r["employee_id"],)) if r else None


# ── 사용자 이력 ─────────────────────────────────────────────────────────
def current_user(vehicle_id: int) -> dict | None:
    return query_one("SELECT a.*, e.name, e.department FROM ga_vehicle_assign a LEFT JOIN employees e ON e.id=a.employee_id "
                     "WHERE a.vehicle_id=? AND a.end_date IS NULL ORDER BY a.start_date DESC, a.id DESC LIMIT 1", (vehicle_id,))


def current_users() -> dict[int, dict]:
    rows = query("SELECT a.vehicle_id, a.employee_id, a.start_date, e.name, e.department FROM ga_vehicle_assign a "
                 "LEFT JOIN employees e ON e.id=a.employee_id WHERE a.end_date IS NULL ORDER BY a.start_date, a.id")
    return {r["vehicle_id"]: r for r in rows}


def assign_history(vehicle_id: int) -> list[dict]:
    return query("SELECT a.*, e.name, e.department FROM ga_vehicle_assign a LEFT JOIN employees e ON e.id=a.employee_id "
                 "WHERE a.vehicle_id=? ORDER BY a.start_date DESC, a.id DESC", (vehicle_id,))


def set_user(vehicle_id: int, employee_id: int | None, on_date: str, note: str = "", inspection_id=None, by: str = ""):
    """현재 사용자를 끝내고(전날까지) 새 사용자를 시작합니다. employee_id 가 없으면 반납(사용자 없음)."""
    on_date = (to_date(on_date) or today()).isoformat()
    cur = current_user(vehicle_id)
    if cur and employee_id and cur["employee_id"] == employee_id:
        return
    if cur:
        end = max(cur["start_date"], (to_date(on_date) - timedelta(days=1)).isoformat())
        execute("UPDATE ga_vehicle_assign SET end_date=? WHERE id=?", (end, cur["id"]))
    if employee_id:
        execute("INSERT INTO ga_vehicle_assign(vehicle_id, employee_id, start_date, inspection_id, note, created_by) "
                "VALUES (?,?,?,?,?,?)", (vehicle_id, employee_id, on_date, inspection_id, note, by))
    plate = query_one("SELECT plate_no FROM ga_vehicles WHERE id=?", (vehicle_id,))["plate_no"]
    audit("ga", "차량 사용자 변경", plate, f"{emp_name(cur['employee_id']) if cur else '-'} → {emp_name(employee_id) or '반납'} ({on_date})")


def user_on(vehicle_id: int, day: str) -> dict | None:
    return query_one("SELECT a.employee_id, e.name FROM ga_vehicle_assign a LEFT JOIN employees e ON e.id=a.employee_id "
                     "WHERE a.vehicle_id=? AND a.start_date<=? AND (a.end_date IS NULL OR a.end_date>=?) "
                     "ORDER BY a.start_date DESC LIMIT 1", (vehicle_id, day, day))


def drivers_between(vehicle_id: int, start: str | None, end: str) -> list[dict]:
    """두 날짜 사이 차를 쓴 사람: 사용자 이력 + 운행기록 운전자 (운행 횟수 많은 순)."""
    start = start or "0000-00-00"
    out: dict = {}
    for a in query("SELECT a.employee_id, e.name, a.start_date, a.end_date FROM ga_vehicle_assign a "
                   "LEFT JOIN employees e ON e.id=a.employee_id WHERE a.vehicle_id=? AND a.employee_id IS NOT NULL "
                   "AND a.start_date<=? AND (a.end_date IS NULL OR a.end_date>=?)", (vehicle_id, end, start)):
        out[a["employee_id"]] = {"employee_id": a["employee_id"], "name": a["name"], "trips": 0, "assigned": True,
                                 "period": f"{max(a['start_date'], start)} ~ {min(a['end_date'] or end, end)}"}
    for l in query("SELECT l.driver_id, e.name, COUNT(*) n, MIN(l.drive_date) d1, MAX(l.drive_date) d2 FROM ga_vehicle_logs l "
                   "JOIN employees e ON e.id=l.driver_id WHERE l.vehicle_id=? AND l.drive_date>=? AND l.drive_date<=? "
                   "GROUP BY l.driver_id", (vehicle_id, start, end)):
        d = out.setdefault(l["driver_id"], {"employee_id": l["driver_id"], "name": l["name"], "trips": 0,
                                            "assigned": False, "period": ""})
        d["trips"] = l["n"]
        d["last"] = l["d2"]
    return sorted(out.values(), key=lambda d: (-d["assigned"], -d["trips"]))


# ── 사진 점검 ───────────────────────────────────────────────────────────
def get_inspection(iid: int) -> dict | None:
    r = query_one("SELECT i.*, v.plate_no, v.model, f.name from_name, t.name to_name, r.name responsible_name "
                  "FROM ga_vehicle_inspections i JOIN ga_vehicles v ON v.id=i.vehicle_id "
                  "LEFT JOIN employees f ON f.id=i.from_emp_id LEFT JOIN employees t ON t.id=i.to_emp_id "
                  "LEFT JOIN employees r ON r.id=i.responsible_id WHERE i.id=?", (iid,))
    return r


def inspections(vehicle_id: int | None = None, status: str = "") -> list[dict]:
    sql = ("SELECT i.*, v.plate_no, v.model, f.name from_name, t.name to_name, "
           "(SELECT COUNT(*) FROM ga_vehicle_photos p WHERE p.inspection_id=i.id) photo_count, "
           "(SELECT COUNT(*) FROM ga_vehicle_photos p WHERE p.inspection_id=i.id AND p.verdict='새파손') new_damage "
           "FROM ga_vehicle_inspections i JOIN ga_vehicles v ON v.id=i.vehicle_id "
           "LEFT JOIN employees f ON f.id=i.from_emp_id LEFT JOIN employees t ON t.id=i.to_emp_id WHERE 1=1")
    p = []
    if vehicle_id:
        sql += " AND i.vehicle_id=?"; p.append(vehicle_id)
    if status:
        sql += " AND i.status=?"; p.append(status)
    return query(sql + " ORDER BY i.insp_date DESC, i.id DESC", p)


def open_inspections(vehicle_id: int) -> list[dict]:
    return [i for i in inspections(vehicle_id) if i["status"] != "확정"]


def baseline_for(vehicle_id: int, before_id: int | None = None) -> dict | None:
    """비교 기준: 가장 최근에 확정된 점검 (파손 신고는 제외 — 8면 사진이 없음)."""
    sql = "SELECT id FROM ga_vehicle_inspections WHERE vehicle_id=? AND status='확정' AND kind!=?"
    p = [vehicle_id, REPORT_KIND]
    if before_id:
        sql += " AND id<?"; p.append(before_id)
    r = query_one(sql + " ORDER BY insp_date DESC, id DESC LIMIT 1", p)
    return get_inspection(r["id"]) if r else None


def create_inspection(vehicle_id: int, kind: str, insp_date: str = "", from_emp_id=None, to_emp_id=None,
                      note: str = "", by: str = "") -> tuple[int | None, str]:
    if kind not in KINDS + [REPORT_KIND]:
        return None, "점검 구분을 고르세요"
    cur = current_user(vehicle_id)
    if kind in ("사용자변경", "반납") and not from_emp_id:
        from_emp_id = cur["employee_id"] if cur else None
    if kind in ("신차인수", "사용자변경") and not to_emp_id:
        return None, "인수자(새 사용자)를 고르세요"
    if kind in ("신차인수", "사용자변경", "반납"):
        busy = [i for i in open_inspections(vehicle_id) if i["kind"] in ("신차인수", "사용자변경", "반납")]
        if busy:
            return None, f"진행 중인 {busy[0]['kind']} 점검({busy[0]['insp_date']})이 있습니다. 먼저 확정하거나 삭제하세요"
    base = baseline_for(vehicle_id)
    iid = execute("INSERT INTO ga_vehicle_inspections(vehicle_id, kind, insp_date, from_emp_id, to_emp_id, note, prev_id, "
                  "created_by) VALUES (?,?,?,?,?,?,?,?)",
                  (vehicle_id, kind, (to_date(insp_date) or today()).isoformat(), _int(from_emp_id) or None,
                   _int(to_emp_id) or None, (note or "").strip()[:300], base["id"] if base else None, by))
    plate = query_one("SELECT plate_no FROM ga_vehicles WHERE id=?", (vehicle_id,))["plate_no"]
    audit("ga", "차량 점검 시작", plate, f"{kind} #{iid} ({by})")
    return iid, ""


def photo_rows(iid: int) -> list[dict]:
    return query("SELECT * FROM ga_vehicle_photos WHERE inspection_id=? ORDER BY id", (iid,))


def slot_map(iid: int | None) -> dict[str, dict]:
    """슬롯별 최신 사진 (파손 부위는 제외)."""
    if not iid:
        return {}
    return {p["slot"]: p for p in photo_rows(iid) if p["slot"] != "damage"}


def add_photo(iid: int, slot: str, raw: bytes, note: str = "", by: str = "") -> tuple[dict | None, str]:
    i = get_inspection(iid)
    if not i:
        return None, "점검을 찾을 수 없습니다"
    if i["status"] == "확정":
        return None, "확정된 점검에는 사진을 더할 수 없습니다"
    if slot not in SLOT_LABEL:
        return None, "사진 위치가 올바르지 않습니다"
    if slot == "damage" and not (note or "").strip():
        note = "파손 부위"
    try:
        main, thumb = photos.encode_vehicle(raw)
    except photos.PhotoError as ex:
        return None, str(ex)
    old = None if slot == "damage" else query_one(
        "SELECT id FROM ga_vehicle_photos WHERE inspection_id=? AND slot=?", (iid, slot))
    pid = execute("INSERT INTO ga_vehicle_photos(inspection_id, slot, note, size, sha256, uploaded_by) VALUES (?,?,?,?,?,?)",
                  (iid, slot, (note or "").strip()[:200], len(main), hashlib.sha256(main).hexdigest(), by))
    photos.save_vehicle(iid, pid, main, thumb)
    if old:                                     # 같은 위치를 다시 찍으면 바꿈 (확정 전에만)
        execute("DELETE FROM ga_vehicle_photos WHERE id=?", (old["id"],))
        photos.delete_vehicle(iid, old["id"])
    audit("ga", "차량 점검 사진", f"{i['plate_no']} #{iid}", f"{SLOT_LABEL[slot]} {len(main) // 1024}KB ({by})")
    return query_one("SELECT * FROM ga_vehicle_photos WHERE id=?", (pid,)), ""


def delete_photo(iid: int, pid: int, by: str = "") -> str:
    i = get_inspection(iid)
    p = query_one("SELECT * FROM ga_vehicle_photos WHERE id=? AND inspection_id=?", (pid, iid))
    if not i or not p:
        return "사진을 찾을 수 없습니다"
    if i["status"] == "확정":
        return "확정된 점검의 사진은 지울 수 없습니다"
    execute("DELETE FROM ga_vehicle_photos WHERE id=?", (pid,))
    photos.delete_vehicle(iid, pid)
    audit("ga", "차량 점검 사진 삭제", f"{i['plate_no']} #{iid}", f"{SLOT_LABEL.get(p['slot'], p['slot'])} ({by})")
    return ""


def missing_slots(iid: int) -> list[str]:
    have = slot_map(iid)
    return [label for key, label in SLOTS if key not in have]


def save_meta(iid: int, odometer=None, fuel_level: str = "", note: str | None = None):
    i = get_inspection(iid)
    if not i or i["status"] == "확정":
        return
    execute("UPDATE ga_vehicle_inspections SET odometer=?, fuel_level=?, note=COALESCE(?, note) WHERE id=?",
            (_int(odometer) or None, (fuel_level or "")[:20], note[:300] if note is not None else None, iid))


def finish_shooting(iid: int, by: str = "") -> str:
    i = get_inspection(iid)
    if not i or i["status"] != "촬영중":
        return ""
    if i["kind"] == REPORT_KIND:
        if not any(p["slot"] == "damage" for p in photo_rows(iid)):
            return "파손 부위 사진을 1장 이상 찍어 주세요"
    else:
        miss = missing_slots(iid)
        if miss:
            return "아직 찍지 않은 사진: " + ", ".join(miss)
    execute("UPDATE ga_vehicle_inspections SET status='확인대기', shot_done_at=? WHERE id=?", (_now(), iid))
    audit("ga", "차량 점검 촬영 완료", f"{i['plate_no']} #{iid}", by)
    return ""


def confirm_party(iid: int, side: str, by: str) -> str:
    """인계자(from)·인수자(to) 확인. 관리부 입회 확인도 같은 칸에 '(입회)'로 남깁니다."""
    i = get_inspection(iid)
    if not i or side not in ("from", "to"):
        return "점검을 찾을 수 없습니다"
    if i["status"] == "촬영중":
        return "사진을 다 찍은 뒤 확인할 수 있습니다"
    if i[f"{side}_ok_at"]:
        return ""
    execute(f"UPDATE ga_vehicle_inspections SET {side}_ok_at=?, {side}_ok_by=? WHERE id=?", (_now(), by[:60], iid))
    audit("ga", "차량 점검 확인", f"{i['plate_no']} #{iid}", f"{'인계자' if side == 'from' else '인수자'}: {by}")
    return ""


def finalize(iid: int, verdicts: dict[int, str], notes: dict[int, str], responsible_id, by: str) -> tuple[str, list[int]]:
    """관리부 확정: 사진별 판정 저장, 새 파손은 사고·파손 기록 생성, 인수인계는 사용자 이력 반영."""
    i = get_inspection(iid)
    if not i:
        return "점검을 찾을 수 없습니다", []
    if i["status"] == "확정":
        return "이미 확정된 점검입니다", []
    if i["status"] == "촬영중":
        return "사진 촬영이 끝나지 않았습니다 (촬영 완료 후 확정)", []
    rows = photo_rows(iid)
    new_damage = []
    for p in rows:
        v = verdicts.get(p["id"]) or ("새파손" if p["slot"] == "damage" and i["kind"] == REPORT_KIND else "이상없음")
        if v not in VERDICTS:
            v = "이상없음"
        note = (notes.get(p["id"]) if notes.get(p["id"]) is not None else p["note"]) or ""
        execute("UPDATE ga_vehicle_photos SET verdict=?, note=? WHERE id=?", (v, note[:200], p["id"]))
        if v == "새파손":
            new_damage.append({**p, "note": note})
    resp = _int(responsible_id) or None
    if new_damage and not resp:
        return "새 파손이 있으면 책임 확인 대상자를 고르세요 (모르면 '확인 필요' 선택)", []
    if resp == -1:
        resp = None
    created = []
    for p in new_damage:
        created.append(execute(
            "INSERT INTO ga_vehicle_damages(vehicle_id, occur_date, kind, part, detail, driver_id, inspection_id, photo_id, "
            "reported_by, note) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (i["vehicle_id"], i["insp_date"], "파손", SLOT_LABEL.get(p["slot"], ""), p["note"] or "", resp, iid, p["id"],
             by, f"{i['kind']} 점검 #{iid}에서 확인")))
    execute("UPDATE ga_vehicle_inspections SET status='확정', result=?, responsible_id=?, reviewed_at=?, reviewed_by=? WHERE id=?",
            ("새파손" if new_damage else "이상없음", resp, _now(), by, iid))
    if i["kind"] in ("신차인수", "사용자변경"):
        set_user(i["vehicle_id"], i["to_emp_id"], i["insp_date"], f"{i['kind']} 점검 #{iid}", iid, by)
    elif i["kind"] == "반납":
        set_user(i["vehicle_id"], None, i["insp_date"], f"반납 점검 #{iid}", iid, by)
    audit("ga", "차량 점검 확정", f"{i['plate_no']} #{iid}", f"{i['kind']} · 새 파손 {len(new_damage)}건")
    return "", created


def delete_inspection(iid: int, by: str) -> str:
    i = get_inspection(iid)
    if not i:
        return "점검을 찾을 수 없습니다"
    if i["status"] == "확정":
        return "확정된 점검은 지울 수 없습니다 (책임 판단 근거)"
    for p in photo_rows(iid):
        photos.delete_vehicle(iid, p["id"])
    execute("DELETE FROM ga_vehicle_inspections WHERE id=?", (iid,))
    audit("ga", "차량 점검 삭제", f"{i['plate_no']} #{iid}", f"{i['kind']} ({by})")
    return ""


def compare_rows(i: dict) -> list[dict]:
    base = get_inspection(i["prev_id"]) if i.get("prev_id") else None
    now, before = slot_map(i["id"]), slot_map(base["id"] if base else None)
    return [{"slot": k, "label": label, "now": now.get(k), "before": before.get(k)} for k, label in SLOTS]


def known_damages(vehicle_id: int) -> list[dict]:
    """아직 수리·종결되지 않은 파손 (새 사용자에게 책임이 없는 '기존 파손')."""
    return query("SELECT d.*, e.name driver_name FROM ga_vehicle_damages d LEFT JOIN employees e ON e.id=d.driver_id "
                 "WHERE d.vehicle_id=? AND d.status IN ('접수','수리중') ORDER BY d.occur_date DESC", (vehicle_id,))


def photo_belongs(pid: int, vehicle_id: int | None = None) -> dict | None:
    p = query_one("SELECT p.*, i.vehicle_id FROM ga_vehicle_photos p JOIN ga_vehicle_inspections i ON i.id=p.inspection_id "
                  "WHERE p.id=?", (pid,))
    if not p or (vehicle_id and p["vehicle_id"] != vehicle_id):
        return None
    return p


# ── 사고·파손 ───────────────────────────────────────────────────────────
def damages(vehicle_id: int | None = None, status: str = "", year: str = "") -> list[dict]:
    sql = ("SELECT d.*, v.plate_no, v.model, e.name driver_name, e.department driver_dept FROM ga_vehicle_damages d "
           "JOIN ga_vehicles v ON v.id=d.vehicle_id LEFT JOIN employees e ON e.id=d.driver_id WHERE 1=1")
    p = []
    if vehicle_id:
        sql += " AND d.vehicle_id=?"; p.append(vehicle_id)
    if status == "진행":
        sql += " AND d.status IN ('접수','수리중')"
    elif status:
        sql += " AND d.status=?"; p.append(status)
    if year:
        sql += " AND substr(d.occur_date,1,4)=?"; p.append(str(year))
    return query(sql + " ORDER BY d.occur_date DESC, d.id DESC", p)


def save_damage(f: dict, by: str) -> int:
    vals = {
        "vehicle_id": _int(f.get("vehicle_id")), "occur_date": (to_date(f.get("occur_date")) or today()).isoformat(),
        "kind": f.get("kind") if f.get("kind") in DAMAGE_KINDS else "파손", "part": (f.get("part") or "")[:60],
        "detail": (f.get("detail") or "")[:300], "driver_id": _int(f.get("driver_id")) or None,
        "insurance": f.get("insurance") if f.get("insurance") in INSURANCE else "미정",
        "repair_cost": max(0, _int(f.get("repair_cost"))), "self_pay": max(0, _int(f.get("self_pay"))),
        "responsibility": f.get("responsibility") if f.get("responsibility") in RESPONSIBILITY else "미정",
        "status": f.get("status") if f.get("status") in DAMAGE_STATUSES else "접수", "note": (f.get("note") or "")[:300],
    }
    vals["self_pay"] = min(vals["self_pay"], vals["repair_cost"]) if vals["repair_cost"] else vals["self_pay"]
    did = _int(f.get("id"))
    cols = list(vals)
    if did:
        execute(f"UPDATE ga_vehicle_damages SET {', '.join(c + '=?' for c in cols)} WHERE id=?", [vals[c] for c in cols] + [did])
    else:
        did = execute(f"INSERT INTO ga_vehicle_damages({', '.join(cols)}, reported_by) VALUES ({', '.join('?' * len(cols))}, ?)",
                      [vals[c] for c in cols] + [by])
    audit("ga", "사고·파손 저장", f"#{did}", f"{vals['kind']} {vals['status']} 수리비 {vals['repair_cost']:,}원 본인 {vals['self_pay']:,}원")
    return did


# ── 과태료 ──────────────────────────────────────────────────────────────
def fines(vehicle_id: int | None = None, status: str = "", year: str = "") -> list[dict]:
    sql = ("SELECT f.*, v.plate_no, v.model, e.name driver_name, e.department driver_dept FROM ga_vehicle_fines f "
           "JOIN ga_vehicles v ON v.id=f.vehicle_id LEFT JOIN employees e ON e.id=f.driver_id WHERE 1=1")
    p = []
    if vehicle_id:
        sql += " AND f.vehicle_id=?"; p.append(vehicle_id)
    if status == "진행":
        sql += f" AND f.status IN ({','.join('?' * len(FINE_OPEN))})"; p.extend(FINE_OPEN)
    elif status:
        sql += " AND f.status=?"; p.append(status)
    if year:
        sql += " AND substr(f.violation_at,1,4)=?"; p.append(str(year))
    rows = query(sql + " ORDER BY f.violation_at DESC, f.id DESC", p)
    for r in rows:
        r["due_dday"] = days_until(r["due_date"])
    return rows


def find_driver(vehicle_id: int, violation_at: str) -> dict:
    """위반 일시의 운전자 찾기: 그날 운행기록 → 그날의 차량 사용자 순."""
    day = (violation_at or "")[:10]
    logs = query("SELECT l.driver_id, e.name, COUNT(*) n FROM ga_vehicle_logs l JOIN employees e ON e.id=l.driver_id "
                 "WHERE l.vehicle_id=? AND l.drive_date=? GROUP BY l.driver_id ORDER BY n DESC", (vehicle_id, day))
    if len(logs) == 1:
        return {"driver_id": logs[0]["driver_id"], "source": "운행기록", "candidates": logs, "message": f"그날 운행기록: {logs[0]['name']}"}
    if len(logs) > 1:
        return {"driver_id": None, "source": "", "candidates": logs,
                "message": "그날 운전자가 여러 명입니다: " + ", ".join(f"{l['name']}({l['n']}회)" for l in logs)}
    u = user_on(vehicle_id, day) if day else None
    if u and u["employee_id"]:
        return {"driver_id": u["employee_id"], "source": "사용자", "candidates": [], "message": f"그날 차량 사용자: {u['name']} (운행기록 없음)"}
    return {"driver_id": None, "source": "", "candidates": [], "message": "그날 운행기록과 사용자 이력이 없습니다. 직접 고르세요"}


def save_fine(f: dict) -> tuple[int, str]:
    vid = _int(f.get("vehicle_id"))
    at = (f.get("violation_at") or "").replace("T", " ")[:16]
    if not vid or not to_date(at[:10]):
        return 0, "차량과 위반 일시를 입력하세요"
    driver, source, msg = _int(f.get("driver_id")) or None, "직접", ""
    if not driver:
        found = find_driver(vid, at)
        driver, source, msg = found["driver_id"], found["source"], found["message"]
    status = f.get("status") if f.get("status") in FINE_STATUSES else "접수"
    paid = f.get("paid_date") or (today().isoformat() if status in ("납부완료", "급여공제") else None)
    vals = {"vehicle_id": vid, "violation_at": at, "kind": f.get("kind") if f.get("kind") in FINE_KINDS else "기타",
            "place": (f.get("place") or "")[:100], "amount": max(0, _int(f.get("amount"))),
            "due_date": (to_date(f.get("due_date")) or None) and to_date(f.get("due_date")).isoformat(),
            "driver_id": driver, "driver_source": source if driver else "", "status": status,
            "paid_date": paid, "note": (f.get("note") or "")[:300]}
    fid = _int(f.get("id"))
    cols = list(vals)
    if fid:
        execute(f"UPDATE ga_vehicle_fines SET {', '.join(c + '=?' for c in cols)} WHERE id=?", [vals[c] for c in cols] + [fid])
    else:
        fid = execute(f"INSERT INTO ga_vehicle_fines({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", [vals[c] for c in cols])
    audit("ga", "과태료 저장", f"#{fid}", f"{vals['kind']} {vals['amount']:,}원 {status} 운전자 {emp_name(driver) or '미확인'}")
    return fid, msg


# ── 비용 분석 ───────────────────────────────────────────────────────────
def months_in_year(v: dict, year: int) -> int:
    start = max(to_date(v.get("acquired_date")) or to_date(f"{year}-01-01"), to_date(f"{year}-01-01"))
    end = min(today(), to_date(f"{year}-12-31"))
    if start > end:
        return 0
    return (end.year - start.year) * 12 + end.month - start.month + 1


def cost_table(year: int) -> dict:
    vehicles = [v for v in repo.list_vehicles() if v["status"] != "매각"]
    logs = repo.vehicle_logs(year=year)
    maint = repo.vehicle_maint(year=year)
    dmg = damages(year=str(year))
    fn = fines(year=str(year))
    agg = defaultdict(lambda: defaultdict(int))
    for l in logs:
        a = agg[l["vehicle_id"]]
        a["km"] += l["distance"]; a["fuel"] += (l["fuel_cost"] or 0) + (l["toll_cost"] or 0); a["trips"] += 1
    for m in maint:
        agg[m["vehicle_id"]]["maint"] += vat_split(m["cost"], m.get("cost_vat"))["total"]
    for d in dmg:
        agg[d["vehicle_id"]]["damage"] += max(0, (d["repair_cost"] or 0) - (d["self_pay"] or 0)) if d["insurance"] != "보험처리" else 0
        agg[d["vehicle_id"]]["damage_cnt"] += 1
    for f in fn:
        agg[f["vehicle_id"]]["fine_cnt"] += 1
        agg[f["vehicle_id"]]["fine"] += f["amount"] or 0
    users = current_users()
    rows = []
    for v in vehicles:
        a = agg[v["id"]]
        months = months_in_year(v, year) or 1
        lease = (v["monthly_cost"] or 0) * months if v["ownership"] != "소유" else 0
        total = lease + a["fuel"] + a["maint"] + a["damage"]
        rows.append({"id": v["id"], "plate_no": v["plate_no"], "model": v["model"], "ownership": v["ownership"],
                     "user": (users.get(v["id"]) or {}).get("name") or "", "months": months, "lease": lease,
                     "fuel": a["fuel"], "maint": a["maint"], "damage": a["damage"], "damage_cnt": a["damage_cnt"],
                     "fine_cnt": a["fine_cnt"], "fine": a["fine"], "total": total, "km": a["km"], "trips": a["trips"],
                     "monthly_cost": round(total / months), "monthly_km": round(a["km"] / months),
                     "per_km": round(total / a["km"]) if a["km"] else None})
    per_km = sorted(r["per_km"] for r in rows if r["per_km"])
    hi = per_km[int(len(per_km) * 0.75)] if len(per_km) >= 4 else None
    for r in rows:
        r["flags"] = []
        if r["monthly_km"] < 300:
            r["flags"].append("운행 적음")
        if hi and r["per_km"] and r["per_km"] >= hi:
            r["flags"].append("km당 비용 높음")
    rows.sort(key=lambda r: -r["total"])
    tot = {k: sum(r[k] for r in rows) for k in ("lease", "fuel", "maint", "damage", "total", "km", "fine_cnt")}
    return {"rows": rows, "total": tot, "year": year}


# ── 정비 주기·알림 ──────────────────────────────────────────────────────
def oil_status(v: dict, odometer: int | None = None) -> dict | None:
    if (v.get("fuel") or "") == "전기":
        return None
    interval = _int(v.get("oil_interval_km"), OIL_DEFAULT_KM) or OIL_DEFAULT_KM
    last = query_one("SELECT maint_date, km FROM ga_vehicle_maint WHERE vehicle_id=? AND km IS NOT NULL AND km>0 "
                     "AND (detail LIKE '%오일%' OR category LIKE '%오일%') ORDER BY km DESC LIMIT 1", (v["id"],))
    if not last:
        return None
    odo = odometer if odometer is not None else repo.last_odometer(v["id"])
    left = last["km"] + interval - odo
    return {"last_km": last["km"], "last_date": last["maint_date"], "next_km": last["km"] + interval, "left": left,
            "interval": interval}


def alerts() -> list[dict]:
    """차량 관련 추가 알림 (보험·검사·리스는 기존 총무 알림에 있음)."""
    out = []

    def add(level, cat, name, msg, d=None, link=None):
        out.append({"level": level, "category": cat, "name": name, "message": msg,
                    "dday": days_until(d) if d else None, "link": link, "emp_id": None})
    for v in repo.list_vehicles():
        if v["status"] == "매각":
            continue
        o = oil_status(v)
        if o and o["left"] <= 1000:
            add("danger" if o["left"] < 0 else "warning", "차량정비", v["plate_no"],
                f"엔진오일 교체 {'시기 지남' if o['left'] < 0 else '임박'} — 지난 교체 {o['last_km']:,}km, 남은 거리 {o['left']:,}km",
                link=f"/ga/vehicles/{v['id']}")
    for f in fines(status="진행"):
        if not f["driver_id"]:
            add("warning", "과태료", f["plate_no"], f"{f['violation_at']} {f['kind']} — 운전자 확인 필요", f["due_date"], "/ga/vehicles?tab=fines")
        elif f["due_dday"] is not None and f["due_dday"] <= 7:
            add("danger" if f["due_dday"] < 0 else "warning", "과태료", f["plate_no"],
                f"{f['driver_name']} {f['kind']} {f['amount']:,}원 납부기한", f["due_date"], "/ga/vehicles?tab=fines")
    for i in inspections(status="확인대기"):
        add("info", "차량점검", i["plate_no"], f"{i['kind']} 사진 점검 — 관리부 비교·확정 대기 ({i['insp_date']})",
            link=f"/ga/vehicles/{i['vehicle_id']}/inspections/{i['id']}")
    for d in damages(status="접수"):
        if d["responsibility"] == "미정":
            add("info", "차량파손", d["plate_no"], f"{d['occur_date']} {d['kind']}({d['part'] or ''}) — 책임·처리 방법 미정",
                link="/ga/vehicles?tab=damages")
    for i in inspections(status="촬영중"):
        if (today() - (to_date(i["insp_date"]) or today())).days >= 3:
            add("info", "차량점검", i["plate_no"], f"{i['kind']} 사진 점검이 {i['insp_date']}부터 촬영 중입니다",
                link=f"/ga/vehicles/{i['vehicle_id']}/inspections/{i['id']}")
    return out


def periodic_due(months: int = 3) -> list[dict]:
    """정기 점검(사진)이 오래된 차량: 마지막 확정 점검이 N개월 넘음."""
    out = []
    limit = add_months(today(), -months).isoformat()
    for v in repo.list_vehicles():
        if v["status"] == "매각":
            continue
        last = baseline_for(v["id"])
        if not last or last["insp_date"] < limit:
            out.append({"id": v["id"], "plate_no": v["plate_no"], "last": last["insp_date"] if last else None})
    return out
