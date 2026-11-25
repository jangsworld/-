"""BMS 문서 보관함: 각 메뉴에 붙인 서류·사진을 NAS 의 메뉴별 폴더에 자동으로 저장합니다.

폴더 구조 (archive_root 아래. 비워 두면 nas_root/BMS시스템, 그것도 없으면 data/outbox/BMS시스템):
    {회사}/{모듈}/{메뉴}/{대상}/[{하위}/]{날짜_시각}_{원래 파일 이름}
    예) 본사/총무관리/법인차량/12가3456/과태료/20261123_101500_고지서.pdf
        본사/인사관리/임직원 서류/2026001_홍길동/20261123_101500_졸업증명서.pdf

- 파일은 화면에서 올리는 순간 위 폴더에 저장되고, 대장(attachments 표)에 위치·크기·지문(sha256)·올린 사람을 남깁니다.
- 받을 때는 대장의 지문과 견주어 바뀌지 않은 파일만 내줍니다. 폴더 밖 경로는 읽지도 쓰지도 않습니다.
- 지우면 파일을 없애지 않고 같은 보관함의 '_삭제함' 으로 옮깁니다 (실수로 지운 서류를 되찾을 수 있게).
- 이 프로그램은 보관함의 파일·폴더를 지우지 않습니다. 서류를 옮긴 뒤 비게 된 폴더도 '_빈폴더' 로 옮겨 둡니다.
- 올릴 수 있는 종류는 정해 둔 것뿐이고(PDF·사진·한글·오피스), 내용의 첫머리가 그 종류와 맞는지도 확인합니다.
- 차량 점검 사진은 서버 안에 작게 보관한 것과 별도로, 관리부가 확정한 점검을 JPG 로 같은 구조의 폴더에 내보냅니다.
"""
import time
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
FS_LOCK = threading.RLock()     # 보관함 안에서 옮기기·이름 바꾸기를 한 번에 하나씩
EMPTY = "_빈폴더"             # 서류를 옮긴 뒤 비게 된 폴더를 모아 두는 곳 (지우지 않음)
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
    return r and {"company": r["company"], "target": f"{r['emp_no']}_{r['name']}", "key": r["emp_no"], "title": r["name"], "back": f"/hr/employees/{r['id']}"}


def vehicle_name(plate, model) -> str:
    """차량 폴더 이름: '차량번호 차종' (폴더만 보고 어느 차인지 알 수 있게).
    차량번호·차종 칸에 '…_사용자' 처럼 밑줄 뒤에 사용자 이름이 붙어 있으면 밑줄 앞까지만 씁니다."""
    return f"{plate_key(plate)} {str(model or '').split('_')[0].strip()}".strip()


def plate_key(plate) -> str:
    """차량번호 칸에 '번호_사용자' 로 적혀 있어도 번호만."""
    return str(plate or "").split("_")[0].strip()


def _vehicle(ref_id):
    r = query_one("SELECT id, plate_no, model, company FROM ga_vehicles WHERE id=?", (ref_id,))
    return r and {"company": r["company"], "target": vehicle_name(r["plate_no"], r["model"]), "key": plate_key(r["plate_no"]),
                  "title": f"{r['plate_no']} {r['model'] or ''}".strip(),
                  "back": f"/ga/vehicles/{r['id']}"}


def _fine(ref_id):
    r = query_one("SELECT f.id, f.violation_at, f.kind, v.id vid, v.plate_no, v.model, v.company FROM ga_vehicle_fines f "
                  "JOIN ga_vehicles v ON v.id=f.vehicle_id WHERE f.id=?", (ref_id,))
    return r and {"company": r["company"], "target": vehicle_name(r["plate_no"], r["model"]), "key": plate_key(r["plate_no"]), "sub": "과태료",
                  "title": f"{r['plate_no']} 과태료 {(r['violation_at'] or '')[:10]} {r['kind'] or ''}".strip(),
                  "prefix": (r["violation_at"] or "")[:10], "back": f"/ga/vehicles/{r['vid']}?tab=fines"}


def _damage(ref_id):
    r = query_one("SELECT d.id, d.occur_date, d.kind, d.part, v.id vid, v.plate_no, v.model, v.company FROM ga_vehicle_damages d "
                  "JOIN ga_vehicles v ON v.id=d.vehicle_id WHERE d.id=?", (ref_id,))
    return r and {"company": r["company"], "target": vehicle_name(r["plate_no"], r["model"]), "key": plate_key(r["plate_no"]), "sub": "사고·파손",
                  "title": f"{r['plate_no']} {r['kind']} {r['occur_date']} {r['part'] or ''}".strip(),
                  "prefix": r["occur_date"] or "", "back": f"/ga/vehicles/{r['vid']}?tab=damages"}


def _asset(ref_id):
    r = query_one("SELECT id, asset_no, name, company FROM ga_assets WHERE id=?", (ref_id,))
    return r and {"company": r["company"], "target": f"{r['asset_no']}_{r['name']}", "key": r["asset_no"], "title": f"{r['asset_no']} {r['name']}",
                  "back": f"/ga/assets/{r['id']}"}


def _contract(ref_id):
    r = query_one("SELECT id, title, counterparty, company FROM ga_contracts WHERE id=?", (ref_id,))
    return r and {"company": r["company"], "target": f"{r['title']}", "title": f"{r['title']} ({r['counterparty'] or ''})",
                  "back": f"/ga/contracts/{r['id']}"}


def _doc(ref_id):
    r = query_one("SELECT id, doc_no, title, direction, doc_date, company FROM ga_docs WHERE id=?", (ref_id,))
    return r and {"company": r["company"], "target": f"{(r['doc_date'] or '')[:4]}", "sub": f"{r['doc_no']}_{r['title']}",
                  "title": f"{r['doc_no']} {r['title']}", "back": "/ga/docs"}


def _item(ref_id):
    r = query_one("SELECT id, name, category, company FROM ga_company_items WHERE id=?", (ref_id,))
    return r and {"company": r["company"], "target": f"{r['category']}_{r['name']}", "title": r["name"], "back": f"/ga/contracts/item/{r['id']}"}


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
    "ga.contracts": ("총무관리", "계약·만기 관리", GA_STAFF, _contract, "계약서 원본·변경 계약서"),
    "ga.docs": ("총무관리", "문서수발·관인", GA_STAFF, _doc, "주고받은 공문 스캔본"),
    "ga.company": ("총무관리", "계약·만기 관리", GA_STAFF, _item, "면허증·인허가증·보험증권·인증서"),
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


# ── 폴더가 두 벌 생기지 않게 ───────────────────────────────────────────
#  1) 대상을 등록하면 BMS 가 폴더를 먼저 만들어 둠 (ensure_folder) → 서류를 미리 넣을 때도 같은 폴더를 씀
#  2) 손으로 만든 비슷한 이름의 폴더(차량번호만, 사번만 등)가 있으면 새로 만들지 않고 그 폴더의 이름을 규칙대로 바꿔 씀
#  3) 폴더에 직접 넣어 둔 파일은 복사하지 않고 그 자리 그대로 대장에 올림 (unregistered / adopt)
SKIP_NAMES = {"thumbs.db", "desktop.ini", ".ds_store"}


def _target_dir(menu: str, t: dict) -> str:
    """대상 폴더(하위 폴더 '과태료' 등은 뺀 것)의 상대 경로."""
    return os.path.join(_safe(company.short(company.norm(t.get("company"))), 30), MENUS[menu][0], MENUS[menu][1], _safe(t["target"]))


def _same_thing(name: str, key: str) -> bool:
    """폴더 이름이 그 대상의 것으로 보이는지: 열쇠와 같거나, 열쇠 뒤에 빈칸·밑줄·붙임표가 오는 이름."""
    n, k = name.strip().lower(), key.strip().lower()
    return bool(k) and (n == k or any(n.startswith(k + sep) for sep in (" ", "_", "-", "(")))


def ensure_folder(menu: str, ref_id: int) -> str | None:
    """대상 폴더를 준비하고 상대 경로를 돌려줌. 이미 있으면 그대로, 비슷한 이름의 폴더가 딱 하나 있으면 이름을 바꿔 씀, 없으면 새로 만듦."""
    t = target(menu, ref_id)
    if not t:
        return None
    r = root()
    rel = _target_dir(menu, t)
    full = os.path.normpath(os.path.join(r, rel))
    if not _inside(r, full):
        return None
    try:
        if not os.path.isdir(full):
            parent = os.path.dirname(full)
            twins = []
            if t.get("key") and os.path.isdir(parent):
                twins = [d for d in os.listdir(parent) if os.path.isdir(os.path.join(parent, d)) and _same_thing(d, _safe(t["key"]))]
            if len(twins) == 1:                              # 손으로 만든 폴더 → 규칙에 맞는 이름으로 바꿔서 그대로 씀
                old_rel = os.path.join(os.path.dirname(rel), twins[0]).replace("\\", "/")
                os.replace(os.path.join(parent, twins[0]), full)
                new_rel = rel.replace("\\", "/")
                for a in query("SELECT id, rel_path FROM attachments WHERE rel_path LIKE ?", (old_rel + "/%",)):
                    execute("UPDATE attachments SET rel_path=? WHERE id=?", (new_rel + a["rel_path"][len(old_rel):], a["id"]))
                audit("files", "폴더 이름 정리", twins[0], "→ " + os.path.basename(full))
            else:
                os.makedirs(full, exist_ok=True)
    except OSError:
        log.exception("폴더 준비 실패: %s", rel)
        return None
    return rel


def ensure_quiet(menu: str, ref_id) -> None:
    """등록·수정 화면에서 부르는 용도: 실패해도 저장을 막지 않음."""
    try:
        if ref_id:
            ensure_folder(menu, int(ref_id))
    except Exception:                                   # noqa: BLE001
        log.exception("폴더 준비 실패")


def ensure_all() -> int:
    """지금 관리 중인 대상(재직 직원·보유 차량·유효 계약·관리 중인 면허·보험 등·쓰는 자산)의 폴더를 미리 만들어 둠."""
    n = 0
    t = datetime.now().strftime("%Y-%m-%d")
    jobs = [("hr.employees", "SELECT id FROM employees WHERE resign_date IS NULL OR resign_date='' OR resign_date>=?", (t,)),
            ("ga.vehicles", "SELECT id FROM ga_vehicles WHERE status<>'매각'", ()),
            ("ga.contracts", "SELECT id FROM ga_contracts WHERE status='유효'", ()),
            ("ga.company", "SELECT id FROM ga_company_items WHERE status IN ('유효','갱신중')", ()),
            ("ga.assets", "SELECT id FROM ga_assets WHERE status<>'폐기'", ())]
    for menu, sql, p in jobs:
        for row in query(sql, p):
            if ensure_folder(menu, row["id"]):
                n += 1
    return n


RESERVED_SUBDIRS = {"ga.vehicles": {"과태료", "사고·파손", "점검사진"}}     # 다른 메뉴가 쓰는 하위 폴더 (차량 서류 화면에서는 건너뜀)
SCAN_DEPTH = 5


def unregistered(menu: str, ref_id: int) -> list[dict]:
    """대상 폴더(하위 폴더 포함)에 직접 넣어 둔 파일 중 대장에 없는 것. name 은 대상 폴더 기준 경로('기타서류/파일.pdf').
    BMS 가 만든 계약서·증명서 PDF(BMS_ 로 시작)는 따로 관리하므로 뺌."""
    t = target(menu, ref_id)
    if not t:
        return []
    r = root()
    rel = folder(menu, t).replace("\\", "/")
    full = os.path.normpath(os.path.join(r, rel))
    if not _inside(r, full) or not os.path.isdir(full):
        return []
    known = {a["rel_path"] for a in query("SELECT rel_path FROM attachments WHERE rel_path LIKE ? AND deleted_at IS NULL", (rel + "/%",))}
    skip_top = RESERVED_SUBDIRS.get(menu, set())
    out = []

    def walk(d: str, prefix: str, depth: int):
        try:
            names = sorted(os.listdir(d))
        except OSError:
            return
        for name in names:
            if len(out) >= 300:
                return
            p = os.path.join(d, name)
            low = name.lower()
            if low in SKIP_NAMES or name.startswith((".", "~$", "@")) or low.endswith(".part"):
                continue
            if os.path.isdir(p):
                if depth < SCAN_DEPTH and not (depth == 0 and name in skip_top):
                    walk(p, prefix + name + "/", depth + 1)
                continue
            if name.startswith("BMS_") or os.path.splitext(low)[1] not in KINDS or (rel + "/" + prefix + name) in known:
                continue
            try:
                out.append({"name": prefix + name, "file": name, "sub": prefix.rstrip("/"), "size": os.path.getsize(p)})
            except OSError:
                continue

    walk(full, "", 0)
    return out


def subdirs(menu: str, ref_id: int) -> list[str]:
    """대상 폴더 안의 하위 폴더들(대상 폴더 기준 경로, 빈 폴더 포함). 탐색기 화면에서 폴더를 폴더로 보여 주려고."""
    t = target(menu, ref_id)
    if not t:
        return []
    r = root()
    full = os.path.normpath(os.path.join(r, folder(menu, t)))
    if not _inside(r, full) or not os.path.isdir(full):
        return []
    skip_top = RESERVED_SUBDIRS.get(menu, set())
    out = []

    def walk(d: str, prefix: str, depth: int):
        try:
            names = sorted(os.listdir(d))
        except OSError:
            return
        for name in names:
            if len(out) >= 200 or name.startswith((".", "~$", "@")) or (depth == 0 and name in skip_top):
                continue
            p = os.path.join(d, name)
            if os.path.isdir(p):
                out.append(prefix + name)
                if depth + 1 < SCAN_DEPTH:
                    walk(p, prefix + name + "/", depth + 1)

    walk(full, "", 0)
    return out


def adopt(menu: str, ref_id: int, names: list[str], note: str = "") -> tuple[int, list[str]]:
    """폴더(하위 폴더 포함)에 넣어 둔 파일을 옮기지 않고 그 자리 그대로 대장에 올림. (올린 수, 못 올린 이유들)"""
    t = target(menu, ref_id)
    if not t:
        raise ArchiveError("붙일 대상을 찾을 수 없습니다")
    waiting = {x["name"]: x for x in unregistered(menu, ref_id)}       # 화면이 보여 준 목록 안의 이름만 (그 밖의 경로는 올 수 없음)
    rel = folder(menu, t).replace("\\", "/")
    ok, errs = 0, []
    for name in names:
        w = waiting.get(name)
        if not w:
            continue
        path = os.path.normpath(os.path.join(root(), rel, name))
        if not _inside(os.path.join(root(), rel), path):
            continue
        try:
            with open(path, "rb") as f:
                raw = f.read(MAX_MB * 1024 * 1024 + 1)
            _ext, mime = check(w["file"], raw)
        except (OSError, ArchiveError) as ex:
            errs.append(f"{name}: {ex if isinstance(ex, ArchiveError) else '읽을 수 없습니다'}")
            continue
        execute("INSERT INTO attachments(menu, ref_id, company, rel_path, orig_name, mime, size, sha256, note, uploaded_by, subdir) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (menu, int(ref_id), company.norm(t.get("company")), rel + "/" + name, w["file"][:120], mime, len(raw),
                 hashlib.sha256(raw).hexdigest(), (note or "폴더에 넣어 둔 파일").strip()[:200], current_actor.get()[1], w["sub"] or None))
        ok += 1
    if ok:
        audit("files", "폴더 파일을 대장에 올림", f"{MENUS[menu][1]} {t['title']}"[:120], f"{ok}개")
        pending_clear(menu)
    return ok, errs


# ── 폴더에 새로 들어온 파일 알림 ─────────────────────────────────────────
# 관리 중인 대상의 폴더를 훑어 '대장에 없는 파일'이 있는 대상만 모음. NAS 를 매번 훑지 않도록 잠깐 기억해 두고,
# 기억이 오래되면 화면을 기다리게 하지 않고 뒤에서 다시 훑음.
#   메뉴: (대상 고르기, 상세 화면 주소, 여러 건이 한 폴더를 같이 쓰는지, 목록 화면 주소)
PENDING = {
    "hr.employees": ("SELECT id FROM employees WHERE resign_date IS NULL OR resign_date='' OR resign_date>=date('now','localtime') ORDER BY id DESC",
                     "/hr/employees/%d?tab=files", False, "/hr/employees"),
    "hr.rewards": ("SELECT id FROM rewards ORDER BY id DESC LIMIT 400", "/files/hr.rewards/%d", True, "/hr/rewards"),
    "mgmt.training": ("SELECT id FROM training_courses ORDER BY id DESC LIMIT 200", "/files/mgmt.training/%d", False, "/mgmt/training?tab=courses"),
    "ga.vehicles": ("SELECT id FROM ga_vehicles WHERE status<>'매각' ORDER BY id DESC", "/ga/vehicles/%d", False, "/ga/vehicles"),
    "ga.fines": ("SELECT id FROM ga_vehicle_fines ORDER BY id DESC LIMIT 400", "/files/ga.fines/%d", True, "/ga/vehicles?tab=fines"),
    "ga.damages": ("SELECT id FROM ga_vehicle_damages ORDER BY id DESC LIMIT 400", "/files/ga.damages/%d", True, "/ga/vehicles?tab=damages"),
    "ga.assets": ("SELECT id FROM ga_assets WHERE status<>'폐기' ORDER BY id DESC LIMIT 1500", "/ga/assets/%d", False, "/ga/assets"),
    "ga.contracts": ("SELECT id FROM ga_contracts WHERE status='유효' ORDER BY id DESC", "/ga/contracts/%d", False, "/ga/contracts"),
    "ga.docs": ("SELECT id FROM ga_docs ORDER BY id DESC LIMIT 400", "/files/ga.docs/%d", False, "/ga/docs"),
    "ga.company": ("SELECT id FROM ga_company_items WHERE status IN ('유효','갱신중') ORDER BY id DESC", "/ga/contracts/item/%d", False, "/ga/contracts"),
}
PENDING_TTL = 30             # 초 (NAS 폴더를 화면 열 때마다 훑지 않도록 잠깐만 기억)
_pending_cache: dict[str, tuple[float, list[dict]]] = {}
_pending_busy: set[str] = set()
_pending_lock = threading.Lock()


def pending_clear(menu: str | None = None):
    if menu:
        _pending_cache.pop(menu, None)
    else:
        _pending_cache.clear()


def _pending_scan(menu: str) -> list[dict]:
    sql, url, shared, list_url = PENDING[menu]
    rows, seen = [], {}
    for r in query(sql):
        try:
            t = target(menu, r["id"])
            if not t:
                continue
            rel = folder(menu, t).replace("\\", "/")
            if rel in seen:                                  # 한 폴더를 같이 쓰는 건(같은 차량의 과태료 등): 폴더는 한 번만 훑음
                if seen[rel] is not None:
                    seen[rel]["ids"].append(r["id"])
                continue
            files = unregistered(menu, r["id"])
        except Exception:                                   # noqa: BLE001  (NAS 가 잠깐 안 보여도 화면은 떠야 함)
            log.exception("새 파일 확인 실패: %s %s", menu, r["id"])
            continue
        seen[rel] = None
        if files:
            parts = rel.split("/")
            link = url % r["id"]
            seen[rel] = {"menu": menu, "ref_id": r["id"], "ids": [r["id"]], "company": company.norm(t.get("company")), "n": len(files),
                         "title": (" › ".join(parts[-2:]) if shared else t["title"]), "names": [f["name"] for f in files[:3]],
                         "shared": shared, "url": list_url if shared else link + ("" if link.startswith("/files/") else "#files")}
            rows.append(seen[rel])
    return rows


def _pending_refresh(menu: str):
    try:
        _pending_cache[menu] = (time.monotonic(), _pending_scan(menu))
    except Exception:                                       # noqa: BLE001
        log.exception("새 파일 확인 실패: %s", menu)
    finally:
        with _pending_lock:
            _pending_busy.discard(menu)


def pending(menus, fresh: bool = False) -> list[dict]:
    """폴더에 넣어 두었지만 대장에 없는 파일이 있는 대상 목록.
    [{menu, ref_id, ids, title, n, names, url, company, shared}] — shared 는 여러 건이 한 폴더를 같이 써서 어느 건의 파일인지 사람이 골라야 하는 것."""
    out = []
    for menu in menus:
        if menu not in PENDING:
            continue
        hit = _pending_cache.get(menu)
        if fresh or not hit:
            hit = _pending_cache[menu] = (time.monotonic(), _pending_scan(menu))
        elif time.monotonic() - hit[0] > PENDING_TTL:        # 오래된 기억: 지금은 그대로 보여 주고 뒤에서 다시 훑음
            with _pending_lock:
                start = menu not in _pending_busy
                _pending_busy.add(menu)
            if start:
                threading.Thread(target=_pending_refresh, args=(menu,), name="bms-pending", daemon=True).start()
        out += hit[1]
    return company.keep_co(out)


def pending_map(rows) -> dict:
    """{(menu, 대상 번호): 새 파일 수} — 목록 줄에 '새 파일' 배지를 붙일 때."""
    return {(p["menu"], i): p["n"] for p in rows for i in p["ids"]}


def save(menu: str, ref_id: int, orig_name: str, raw: bytes, note: str = "", subdir: str = "") -> int:
    """subdir: 대상 폴더 안의 하위 폴더 (archive_ops.clean_dir 로 확인한 것만)."""
    t = target(menu, ref_id)
    if not t:
        raise ArchiveError("붙일 대상을 찾을 수 없습니다")
    ensure_folder(menu, ref_id)                              # 손으로 만든 비슷한 폴더가 있으면 먼저 그 폴더로 맞춤
    ext, mime = check(orig_name, raw)
    base = _safe(os.path.splitext(os.path.basename(orig_name))[0], 50)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    head = (t.get("prefix") + "_") if t.get("prefix") else ""
    sha = hashlib.sha256(raw).hexdigest()
    where = os.path.join(folder(menu, t), *subdir.split("/")) if subdir else folder(menu, t)
    rel = os.path.join(where, f"{head}{stamp}_{base}{ext}")
    n = 1
    while os.path.exists(os.path.join(root(), rel)):                      # 같은 초에 같은 이름: 번호를 붙임
        n += 1
        rel = os.path.join(where, f"{head}{stamp}_{base}_{n}{ext}")
    _write(rel, raw)
    att = execute("INSERT INTO attachments(menu, ref_id, company, rel_path, orig_name, mime, size, sha256, note, uploaded_by, subdir) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (menu, int(ref_id), company.norm(t.get("company")), rel.replace("\\", "/"), _safe(os.path.basename(orig_name), 120), mime,
                   len(raw), sha, (note or "").strip()[:200] or None, current_actor.get()[1], subdir or None))
    audit("files", "서류 붙임", f"{MENUS[menu][1]} {t['title']}"[:120], f"{base}{ext} {len(raw) // 1024}KB")
    return att


async def save_uploads(menu: str, ref_id: int, uploads) -> str:
    """등록 창에서 함께 올린 파일들을 보관함에 저장하고, 화면에 덧붙일 안내 문구를 돌려줌 (없으면 빈 글자)."""
    from fastapi.concurrency import run_in_threadpool
    ok, errs = 0, []
    for up in uploads or []:
        name = getattr(up, "filename", "") or ""
        if not name:
            continue
        raw = await up.read(MAX_MB * 1024 * 1024 + 1)
        try:
            await run_in_threadpool(save, menu, ref_id, name, raw, "")
            ok += 1
        except ArchiveError as ex:
            errs.append(f"{name}: {ex}")
    return (f" · 서류 {ok}개를 보관함에 저장했습니다" if ok else "") + ((" · 저장하지 못한 서류 — " + " / ".join(errs)) if errs else "")


def items(menu: str, ref_id: int) -> list[dict]:
    rows = query("SELECT * FROM attachments WHERE menu=? AND ref_id=? AND deleted_at IS NULL ORDER BY id DESC", (menu, int(ref_id)))
    for r in rows:
        r["where"] = " \\ ".join(re.split(r"[\\/]+", os.path.dirname(r["rel_path"])))
        r["sub"] = (r.get("subdir") or "").replace("/", " \\ ")
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


def trash_path(rel: str) -> str:
    """'_삭제함' 안의 자리. 같은 이름이 이미 있으면 덮어쓰지 않게 시각을 붙임."""
    dst = os.path.normpath(os.path.join(root(), TRASH, rel))
    if not _inside(os.path.join(root(), TRASH), dst):
        raise ArchiveError("허용되지 않은 경로")
    base, ext = os.path.splitext(dst)
    n = 0
    while os.path.exists(dst):
        n += 1
        dst = base + "_" + datetime.now().strftime("%Y%m%d_%H%M%S") + (f"_{n}" if n > 1 else "") + ext
    return dst


def delete(att_id: int) -> bool:
    """대장에서 지우고 파일은 '_삭제함' 으로 옮김. 옮기지 못하면 대장도 그대로 두고 알림."""
    att = get(att_id)
    if not att:
        return False
    src = os.path.normpath(os.path.join(root(), att["rel_path"]))
    try:
        with FS_LOCK:
            if _inside(root(), src) and os.path.exists(src):
                dst = trash_path(att["rel_path"])
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                os.replace(src, dst)
    except OSError as ex:
        log.exception("삭제함으로 옮기지 못함")
        raise ArchiveError("삭제함으로 옮기지 못했습니다 (다른 사람이 파일을 열어 두었거나 NAS 연결이 끊겼습니다)") from ex
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
    sql = ("SELECT i.id, i.kind, i.insp_date, v.plate_no, v.model, v.company FROM ga_vehicle_inspections i "
           "JOIN ga_vehicles v ON v.id=i.vehicle_id WHERE i.status='확정'")
    p = []
    if inspection_id:
        sql += " AND i.id=?"
        p.append(inspection_id)
    n = 0
    for i in query(sql, p):
        t = {"company": i["company"], "target": vehicle_name(i["plate_no"], i["model"]), "sub": "점검사진"}
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


def _has_files(path: str) -> bool:
    for _d, _subs, files in os.walk(path):
        if files:
            return True
    return False


def _tidy_dirs(path: str):
    """서류를 옮긴 뒤 비게 된 예전 폴더를 지우지 않고 보관함의 '_빈폴더' 로 옮김 (원래 경로 그대로, 같은 이름이 있으면 시각을 붙임).
    파일이 하나라도 남아 있으면 건드리지 않습니다. 회사·모듈·메뉴 폴더(위 3단계)는 비어도 그대로 둡니다. 이 프로그램은 폴더를 지우지 않습니다."""
    r = root()
    path = os.path.normpath(path)
    if not _inside(r, path) or not os.path.isdir(path) or _has_files(path):
        return
    depth = lambda d: len(os.path.relpath(d, r).replace("\\", "/").split("/"))
    if depth(path) < 4 or os.path.relpath(path, r).replace("\\", "/").split("/")[0] in (TRASH, EMPTY):
        return
    while depth(os.path.dirname(path)) >= 4 and not _has_files(os.path.dirname(path)):    # 대상 폴더 전체가 비었으면 그 폴더째로
        path = os.path.dirname(path)
    rel = os.path.relpath(path, r)
    dst = os.path.normpath(os.path.join(r, EMPTY, rel))
    if os.path.exists(dst):
        dst += "_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.replace(path, dst)
        audit("files", "빈 폴더 옮김", rel.replace("\\", "/")[:150], "→ " + EMPTY)
    except OSError:
        log.exception("빈 폴더 옮기기 실패: %s", rel)


def rehome() -> int:
    """대상의 이름이 바뀌어(차종을 고침, 회사를 옮김, 폴더 이름 규칙 변경 등) 폴더가 달라진 서류를 새 폴더로 옮기고 대장을 맞춤.
    차량은 예전 폴더에 남은 점검 사진 폴더도 함께 옮깁니다. 옮긴 개수를 돌려줌. 파일이 없거나 옮기지 못하면 그대로 둠."""
    r, n = root(), 0
    for att in query("SELECT id, menu, ref_id, rel_path, subdir FROM attachments WHERE deleted_at IS NULL"):
        t = target(att["menu"], att["ref_id"])
        if not t:
            continue
        base = folder(att["menu"], t).replace("\\", "/")
        want = base + ("/" + att["subdir"] if att.get("subdir") else "")       # 담당자가 만든 하위 폴더는 그대로 둠
        cur_dir, name = os.path.split(att["rel_path"])
        if cur_dir == want:
            continue
        # 대상 폴더를 통째로 새 이름으로 바꿀 수 있으면 그렇게 함 (대장에 없는 파일·하위 폴더도 함께 따라가 폴더가 두 벌이 되지 않게)
        sd = att.get("subdir") or ""
        old_base = cur_dir[:-len(sd) - 1] if sd and cur_dir.endswith("/" + sd) else cur_dir
        ob, nb = os.path.normpath(os.path.join(r, old_base)), os.path.normpath(os.path.join(r, base))
        if old_base != base and _inside(r, ob) and _inside(r, nb) and os.path.isdir(ob) and not os.path.exists(nb)                 and len(old_base.split("/")) >= 4 and old_base.split("/")[0] not in (TRASH, EMPTY):
            try:
                os.makedirs(os.path.dirname(nb), exist_ok=True)
                os.replace(ob, nb)
            except OSError:
                log.exception("폴더 이름 바꾸기 실패: %s", old_base)
            else:
                for a2 in query("SELECT id, rel_path FROM attachments WHERE rel_path LIKE ?", (old_base + "/%",)):
                    execute("UPDATE attachments SET rel_path=? WHERE id=?", (base + a2["rel_path"][len(old_base):], a2["id"]))
                    n += 1
                audit("files", "폴더 이름 정리", old_base.split("/")[-1], "→ " + base.split("/")[-1])
                continue
        src = os.path.normpath(os.path.join(r, att["rel_path"]))
        dst = os.path.normpath(os.path.join(r, want, name))
        if not (_inside(r, src) and _inside(r, dst)) or not os.path.exists(src) or os.path.exists(dst):
            continue
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.replace(src, dst)
        except OSError:
            log.exception("서류 폴더 옮기기 실패 (붙임 %s)", att["id"])
            continue
        execute("UPDATE attachments SET rel_path=? WHERE id=?", (want + "/" + name, att["id"]))
        n += 1
        if att["menu"].startswith("ga.") and "법인차량" in want:           # 예전 차량 폴더의 점검 사진도 새 폴더로
            old_car = os.path.normpath(os.path.join(r, *cur_dir.split("/")[:4]))
            new_car = os.path.normpath(os.path.join(r, *want.split("/")[:4]))
            old_ph, new_ph = os.path.join(old_car, "점검사진"), os.path.join(new_car, "점검사진")
            try:
                if old_car != new_car and os.path.isdir(old_ph) and not os.path.exists(new_ph):
                    os.replace(old_ph, new_ph)
            except OSError:
                log.exception("점검 사진 폴더 옮기기 실패")
        _tidy_dirs(os.path.dirname(src))
    if n:
        audit("files", "서류 폴더 정리", f"{n}개", "대상 이름에 맞춰 옮김")
    return n


def _loop():
    while True:                                     # 시작할 때 한 번, 그 뒤 6시간마다: 폴더 이름 맞추기 + 놓친 차량 사진 내보내기
        try:
            ensure_all()                            # 먼저: 손으로 만든 폴더를 규칙 이름으로 바꿔 그대로 씀 (두 벌이 되지 않게)
            with FS_LOCK:
                rehome()
            export_vehicle_photos()
        except Exception:                           # noqa: BLE001
            log.exception("문서 보관함 정리 실패")
        if _stop.wait(6 * 3600):
            return


def _auto_loop():
    """2분마다: NAS 폴더에 직접 넣은 파일을 대장에 올림 (설정에서 끌 수 있음)."""
    from app import archive_ops
    while not _stop.wait(120):
        try:
            archive_ops.sync_all()
        except Exception:                           # noqa: BLE001
            log.exception("자동 대장 올리기 실패")


def start_worker():
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="bms-archive", daemon=True)
    _thread.start()
    threading.Thread(target=_auto_loop, name="bms-archive-auto", daemon=True).start()


def stop_worker():
    _stop.set()
