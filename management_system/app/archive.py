"""BMS 문서 보관함: 각 메뉴에 붙인 서류·사진을 NAS 의 메뉴별 폴더에 자동으로 저장합니다.

폴더 구조 (archive_root 아래. 비워 두면 nas_root/BMS시스템, 그것도 없으면 data/outbox/BMS시스템):
    {회사}/{모듈}/{메뉴}/{대상}/[{하위}/]{날짜_시각}_{원래 파일 이름}
    예) 본사/총무관리/법인차량/12가3456/과태료/20261123_101500_고지서.pdf
        본사/인사관리/임직원 서류/2026001_홍길동/20261123_101500_졸업증명서.pdf

- 파일은 화면에서 올리는 순간 위 폴더에 저장되고, 대장(attachments 표)에 위치·크기·지문(sha256)·올린 사람을 남깁니다.
- 받을 때는 대장의 지문과 견주어 바뀌지 않은 파일만 내줍니다. 폴더 밖 경로는 읽지도 쓰지도 않습니다.
- 지우면 파일을 없애지 않고 같은 보관함의 '_삭제함' 으로 옮깁니다 (실수로 지운 서류를 되찾을 수 있게).
- 올릴 수 있는 종류는 정해 둔 것뿐이고(PDF·사진·한글·오피스), 내용의 첫머리가 그 종류와 맞는지도 확인합니다.
- 차량 점검 사진은 서버 안에 작게 보관한 것과 별도로, 관리부가 확정한 점검을 JPG 로 같은 구조의 폴더에 내보냅니다.
"""
import hashlib
import io
import logging
import os
import re
import threading
from datetime import datetime

from app import company
from app.config import settings
from app.db import audit, current_actor, execute, query, query_one

log = logging.getLogger("bms.archive")
MAX_MB = 30
TRASH = "_삭제함"
GA_STAFF = {"admin", "hr", "ga"}
WRITERS = {"admin", "hr"}

# 올릴 수 있는 파일 종류: 확장자 → (첫머리 글자들, 내려줄 때의 종류)
KINDS = {
    ".pdf": ((b"%PDF",), "application/pdf"),
    ".jpg": ((b"\xff\xd8\xff",), "image/jpeg"), ".jpeg": ((b"\xff\xd8\xff",), "image/jpeg"),
    ".png": ((b"\x89PNG",), "image/png"),
    ".webp": ((b"RIFF",), "image/webp"),
    ".hwp": ((b"\xd0\xcf\x11\xe0",), "application/octet-stream"),
    ".hwpx": ((b"PK\x03\x04",), "application/octet-stream"),
    ".docx": ((b"PK\x03\x04",), "application/octet-stream"),
    ".xlsx": ((b"PK\x03\x04",), "application/octet-stream"),
    ".pptx": ((b"PK\x03\x04",), "application/octet-stream"),
    ".doc": ((b"\xd0\xcf\x11\xe0",), "application/octet-stream"),
    ".xls": ((b"\xd0\xcf\x11\xe0",), "application/octet-stream"),
}
INLINE = {"application/pdf", "image/jpeg", "image/png", "image/webp"}      # 브라우저에서 바로 보여도 되는 종류


class ArchiveError(Exception):
    pass


def _emp(ref_id):
    r = query_one("SELECT id, emp_no, name, company FROM employees WHERE id=?", (ref_id,))
    return r and {"company": r["company"], "target": f"{r['emp_no']}_{r['name']}", "title": r["name"], "back": f"/hr/employees/{r['id']}"}


def _vehicle(ref_id):
    r = query_one("SELECT id, plate_no, model, company FROM ga_vehicles WHERE id=?", (ref_id,))
    return r and {"company": r["company"], "target": r["plate_no"], "title": f"{r['plate_no']} {r['model'] or ''}".strip(),
                  "back": f"/ga/vehicles/{r['id']}"}


def _fine(ref_id):
    r = query_one("SELECT f.id, f.violation_at, f.kind, v.id vid, v.plate_no, v.company FROM ga_vehicle_fines f "
                  "JOIN ga_vehicles v ON v.id=f.vehicle_id WHERE f.id=?", (ref_id,))
    return r and {"company": r["company"], "target": r["plate_no"], "sub": "과태료",
                  "title": f"{r['plate_no']} 과태료 {(r['violation_at'] or '')[:10]} {r['kind'] or ''}".strip(),
                  "prefix": (r["violation_at"] or "")[:10], "back": f"/ga/vehicles/{r['vid']}?tab=fines"}


def _damage(ref_id):
    r = query_one("SELECT d.id, d.occur_date, d.kind, d.part, v.id vid, v.plate_no, v.company FROM ga_vehicle_damages d "
                  "JOIN ga_vehicles v ON v.id=d.vehicle_id WHERE d.id=?", (ref_id,))
    return r and {"company": r["company"], "target": r["plate_no"], "sub": "사고·파손",
                  "title": f"{r['plate_no']} {r['kind']} {r['occur_date']} {r['part'] or ''}".strip(),
                  "prefix": r["occur_date"] or "", "back": f"/ga/vehicles/{r['vid']}?tab=damages"}


def _asset(ref_id):
    r = query_one("SELECT id, asset_no, name, company FROM ga_assets WHERE id=?", (ref_id,))
    return r and {"company": r["company"], "target": f"{r['asset_no']}_{r['name']}", "title": f"{r['asset_no']} {r['name']}",
                  "back": f"/ga/assets/{r['id']}"}


def _contract(ref_id):
    r = query_one("SELECT id, title, counterparty, company FROM ga_contracts WHERE id=?", (ref_id,))
    return r and {"company": r["company"], "target": f"{r['title']}", "title": f"{r['title']} ({r['counterparty'] or ''})",
                  "back": "/ga/contracts"}


def _doc(ref_id):
    r = query_one("SELECT id, doc_no, title, direction, doc_date, company FROM ga_docs WHERE id=?", (ref_id,))
    return r and {"company": r["company"], "target": f"{(r['doc_date'] or '')[:4]}", "sub": f"{r['doc_no']}_{r['title']}",
                  "title": f"{r['doc_no']} {r['title']}", "back": "/ga/docs"}


def _item(ref_id):
    r = query_one("SELECT id, name, category, company FROM ga_company_items WHERE id=?", (ref_id,))
    return r and {"company": r["company"], "target": f"{r['category']}_{r['name']}", "title": r["name"], "back": "/ga/company"}


def _reward(ref_id):
    r = query_one("SELECT r.id, r.kind, r.action_date reward_date, e.emp_no, e.name, e.company FROM rewards r JOIN employees e ON e.id=r.employee_id "
                  "WHERE r.id=?", (ref_id,))
    return r and {"company": r["company"], "target": f"{r['emp_no']}_{r['name']}", "title": f"{r['name']} {r['kind']} {r['reward_date'] or ''}",
                  "prefix": r["reward_date"] or "", "back": "/hr/rewards"}


def _course(ref_id):
    r = query_one("SELECT id, name FROM training_courses WHERE id=?", (ref_id,))
    return r and {"company": company.cur() if company.cur() in company.IDS else "1", "target": r["name"], "title": r["name"],
                  "back": "/mgmt/training"}


# 메뉴: (모듈 폴더, 메뉴 폴더, 볼 수 있는 권한, 대상 찾기, 설명)
MENUS = {
    "hr.employees": ("인사관리", "임직원 서류", WRITERS, _emp, "입사서류·자격증·통장사본 등 개인 서류"),
    "hr.rewards": ("인사관리", "상벌현황", WRITERS, _reward, "표창장·징계 의결서 등"),
    "mgmt.training": ("인사관리", "교육 이수", GA_STAFF, _course, "수료증·출석부·교육 자료"),
    "ga.vehicles": ("총무관리", "법인차량", GA_STAFF, _vehicle, "자동차등록증·보험증권·리스 계약서 등"),
    "ga.fines": ("총무관리", "법인차량", GA_STAFF, _fine, "과태료·범칙금 고지서, 납부 영수증"),
    "ga.damages": ("총무관리", "법인차량", GA_STAFF, _damage, "사고·파손 사진, 견적서, 수리 영수증, 보험 서류"),
    "ga.assets": ("총무관리", "자산·비품", GA_STAFF, _asset, "구매 영수증·보증서·설명서"),
    "ga.contracts": ("총무관리", "계약·임대차", GA_STAFF, _contract, "계약서 원본·변경 계약서"),
    "ga.docs": ("총무관리", "문서수발·관인", GA_STAFF, _doc, "주고받은 공문 스캔본"),
    "ga.company": ("총무관리", "회사 관리 만기", GA_STAFF, _item, "면허증·인허가증·보험증권·인증서"),
}


def root() -> str:
    return os.path.normpath(getattr(settings, "archive_root", "") or os.path.join(settings.output_root, "BMS시스템"))


def _safe(name: str, limit: int = 60) -> str:
    """폴더·파일 이름으로 쓸 수 있게: 금지 글자·경로 구분자를 없애고 길이를 줄임."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name or "")).strip(" .")
    name = re.sub(r"\s+", " ", name)
    return name[:limit].strip(" .") or "이름없음"


def _inside(base: str, path: str) -> bool:
    try:
        return os.path.commonpath([os.path.normpath(base), os.path.normpath(path)]) == os.path.normpath(base)
    except ValueError:
        return False


def allowed(menu: str, role: str | None) -> bool:
    return menu in MENUS and role in MENUS[menu][2]


def target(menu: str, ref_id: int) -> dict | None:
    if menu not in MENUS:
        return None
    return MENUS[menu][3](ref_id)


def folder(menu: str, t: dict) -> str:
    """보관함 안의 상대 폴더."""
    module, name = MENUS[menu][0], MENUS[menu][1]
    parts = [_safe(company.short(company.norm(t.get("company"))), 30), module, name, _safe(t["target"])]
    if t.get("sub"):
        parts.append(_safe(t["sub"]))
    return os.path.join(*parts)


def check(orig_name: str, raw: bytes) -> tuple[str, str]:
    """(확장자, 종류) 또는 ArchiveError."""
    ext = os.path.splitext(orig_name or "")[1].lower()
    if ext not in KINDS:
        raise ArchiveError("올릴 수 있는 파일은 PDF·사진(JPG·PNG)·한글·워드·엑셀·파워포인트입니다")
    if not raw:
        raise ArchiveError("빈 파일입니다")
    if len(raw) > MAX_MB * 1024 * 1024:
        raise ArchiveError(f"파일이 너무 큽니다 ({MAX_MB}MB 이하)")
    heads, mime = KINDS[ext]
    if not any(raw.startswith(h) for h in heads):
        raise ArchiveError("파일 내용이 확장자와 맞지 않습니다 (이름만 바꾼 파일은 올릴 수 없습니다)")
    return ext, mime


def _write(rel: str, raw: bytes):
    path = os.path.normpath(os.path.join(root(), rel))
    if not _inside(root(), path):
        raise ArchiveError("저장 경로가 올바르지 않습니다")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".part"
        with open(tmp, "wb") as f:
            f.write(raw)
        os.replace(tmp, path)
    except OSError as ex:
        raise ArchiveError(f"보관함에 저장하지 못했습니다: {ex.__class__.__name__} (NAS 연결·폴더 권한을 확인하세요)") from ex


def save(menu: str, ref_id: int, orig_name: str, raw: bytes, note: str = "") -> int:
    t = target(menu, ref_id)
    if not t:
        raise ArchiveError("붙일 대상을 찾을 수 없습니다")
    ext, mime = check(orig_name, raw)
    base = _safe(os.path.splitext(os.path.basename(orig_name))[0], 50)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    head = (t.get("prefix") + "_") if t.get("prefix") else ""
    sha = hashlib.sha256(raw).hexdigest()
    rel = os.path.join(folder(menu, t), f"{head}{stamp}_{base}{ext}")
    n = 1
    while os.path.exists(os.path.join(root(), rel)):                      # 같은 초에 같은 이름: 번호를 붙임
        n += 1
        rel = os.path.join(folder(menu, t), f"{head}{stamp}_{base}_{n}{ext}")
    _write(rel, raw)
    att = execute("INSERT INTO attachments(menu, ref_id, company, rel_path, orig_name, mime, size, sha256, note, uploaded_by) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (menu, int(ref_id), company.norm(t.get("company")), rel.replace("\\", "/"), _safe(os.path.basename(orig_name), 120), mime,
                   len(raw), sha, (note or "").strip()[:200] or None, current_actor.get()[1]))
    audit("files", "서류 붙임", f"{MENUS[menu][1]} {t['title']}"[:120], f"{base}{ext} {len(raw) // 1024}KB")
    return att


def items(menu: str, ref_id: int) -> list[dict]:
    rows = query("SELECT * FROM attachments WHERE menu=? AND ref_id=? AND deleted_at IS NULL ORDER BY id DESC", (menu, int(ref_id)))
    for r in rows:
        r["where"] = " \\ ".join(re.split(r"[\\/]+", os.path.dirname(r["rel_path"])))
        r["inline"] = r["mime"] in INLINE
    return rows


def counts(menu: str) -> dict[int, int]:
    return {r["ref_id"]: r["n"] for r in query(
        "SELECT ref_id, COUNT(*) n FROM attachments WHERE menu=? AND deleted_at IS NULL GROUP BY ref_id", (menu,))}


def get(att_id: int) -> dict | None:
    return query_one("SELECT * FROM attachments WHERE id=? AND deleted_at IS NULL", (att_id,))


def read(att: dict) -> bytes:
    path = os.path.normpath(os.path.join(root(), att["rel_path"]))
    if not _inside(root(), path):
        raise ArchiveError("허용되지 않은 경로")
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as ex:
        raise ArchiveError("파일을 읽을 수 없습니다 (보관함 폴더에서 옮겨졌거나 NAS 연결이 끊겼습니다)") from ex
    if hashlib.sha256(data).hexdigest() != att["sha256"]:
        raise ArchiveError("파일이 올릴 때와 다릅니다 (보관함 폴더에서 바뀌었습니다)")
    return data


def delete(att_id: int) -> bool:
    """대장에서 지우고 파일은 '_삭제함' 으로 옮김."""
    att = get(att_id)
    if not att:
        return False
    src = os.path.normpath(os.path.join(root(), att["rel_path"]))
    dst = os.path.normpath(os.path.join(root(), TRASH, att["rel_path"]))
    try:
        if _inside(root(), src) and _inside(root(), dst) and os.path.exists(src):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.replace(src, dst)
    except OSError:
        log.exception("삭제함으로 옮기지 못함")
    execute("UPDATE attachments SET deleted_at=datetime('now','localtime'), deleted_by=? WHERE id=?", (current_actor.get()[1], att_id))
    audit("files", "서류 삭제", att["orig_name"], f"{att['menu']} #{att['ref_id']} (삭제함으로 옮김)")
    return True


def status() -> dict:
    """보관함 폴더에 닿는지 (설정 화면 표시용)."""
    r = root()
    try:
        os.makedirs(r, exist_ok=True)
        ok = os.access(r, os.W_OK)
    except OSError:
        ok = False
    n = query_one("SELECT COUNT(*) n, COALESCE(SUM(size),0) s FROM attachments WHERE deleted_at IS NULL")
    return {"root": r, "ok": ok, "count": n["n"], "mb": round(n["s"] / 1024 / 1024, 1), "nas": bool(getattr(settings, "archive_root", "") or settings.nas_root)}


# ── 차량 점검 사진 내보내기 ────────────────────────────────────────────
def export_vehicle_photos(inspection_id: int | None = None) -> int:
    """확정한 점검의 사진을 보관함에 JPG 로 내보냄. 이미 있는 파일은 건너뜀. 내보낸 장수를 돌려줌."""
    from PIL import Image
    from app.modules.ga import fleet, photos
    sql = ("SELECT i.id, i.kind, i.insp_date, v.plate_no, v.company FROM ga_vehicle_inspections i "
           "JOIN ga_vehicles v ON v.id=i.vehicle_id WHERE i.status='확정'")
    p = []
    if inspection_id:
        sql += " AND i.id=?"
        p.append(inspection_id)
    n = 0
    for i in query(sql, p):
        t = {"company": i["company"], "target": i["plate_no"], "sub": "점검사진"}
        base = os.path.join(folder("ga.vehicles", t), _safe(f"{i['insp_date']}_{i['kind']}_{i['id']}"))
        for ph in query("SELECT id, slot, note, verdict FROM ga_vehicle_photos WHERE inspection_id=? ORDER BY id", (i["id"],)):
            label = fleet.SLOT_LABEL.get(ph["slot"], ph["slot"])
            extra = "_새파손" if ph.get("verdict") == "새파손" else ""
            rel = os.path.join(base, _safe(f"{label}{extra}_{ph['id']}") + ".jpg")
            full = os.path.normpath(os.path.join(root(), rel))
            if os.path.exists(full):
                continue
            try:
                with Image.open(photos.vehicle_path(i["id"], ph["id"])) as img:
                    buf = io.BytesIO()
                    img.convert("RGB").save(buf, "JPEG", quality=85)
                _write(rel, buf.getvalue())
                n += 1
            except (OSError, ArchiveError):
                log.exception("차량 사진 내보내기 실패 (점검 %s 사진 %s)", i["id"], ph["id"])
    return n


_stop = threading.Event()
_thread: threading.Thread | None = None


def _loop():
    while not _stop.wait(6 * 3600):                 # 놓친 사진이 있으면 6시간마다 다시 내보냄
        try:
            export_vehicle_photos()
        except Exception:                           # noqa: BLE001
            log.exception("차량 사진 내보내기 실패")


def start_worker():
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="bms-archive", daemon=True)
    _thread.start()


def stop_worker():
    _stop.set()
