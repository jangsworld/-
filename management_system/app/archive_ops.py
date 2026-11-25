"""문서 보관함 탐색기 기능: 새 폴더 · 옮기기 · 이름 바꾸기 · 폴더 지우기 · NAS 에 넣은 파일 자동으로 대장에 올리기.

지키는 것
- 움직이는 범위는 '그 건의 폴더 안'뿐. 맨 위 폴더(사번_이름, 차량번호 차종, 계약명)는 이름을 바꾸거나 지울 수 없음.
- 아무것도 지우지 않음: 지우기는 '_삭제함' 으로 옮기기. 같은 이름이 있으면 덮어쓰지 않고 멈추거나 시각을 붙임.
- 모든 동작은 접속기록에 남음. 다른 건의 서류가 섞여 있는 폴더(과태료처럼 폴더를 같이 쓰는 경우)는 폴더째 바꾸지 못함.
"""
import hashlib
import logging
import os
import re
import time
from collections import deque

from app import archive, company
from app.archive import ArchiveError, EMPTY, FS_LOCK, MAX_MB, MENUS, PENDING, TRASH, _inside, check, folder, root, target
from app.db import audit, batch, current_actor, execute, get_settings, query, query_one, set_setting

log = logging.getLogger("bms.archive")
MAX_DEPTH = 5                                 # 대상 폴더 아래로 만들 수 있는 폴더 단계 (archive.SCAN_DEPTH 와 같아야 다 보임)
AUTO_BY = "NAS 자동"                           # 자동으로 올린 서류의 '올린 사람'
AUTO_SETTLE = 8                               # 초. 복사 중인 파일을 올리지 않게, 이만큼 그대로인 파일만
STAMP = re.compile(r"^(?:\d{4}-\d{2}-\d{2}_)?\d{8}_\d{6}_")      # 화면에서 올린 파일의 NAS 이름 앞에 붙는 날짜·시각
_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# ── 이름·경로 확인 ─────────────────────────────────────────────────────
def clean_name(name, limit: int = 60) -> str:
    """폴더·파일 이름 하나. 경로 글자·금지 글자가 있으면 고쳐 쓰지 않고 거절."""
    n = re.sub(r"\s+", " ", str(name or "")).strip()
    if not n:
        raise ArchiveError("이름을 넣어 주세요")
    if len(n) > limit:
        raise ArchiveError(f"이름이 너무 깁니다 ({limit}자 이하)")
    if _BAD.search(n) or n in (".", "..") or n.endswith(".") or n[0] in ".~@" or n.upper().startswith("BMS_") or n in (TRASH, EMPTY):
        raise ArchiveError('이름에 쓸 수 없는 글자가 있습니다 (< > : " / \\ | ? * 와 점·~·@ 로 시작하는 이름, BMS_ 로 시작하는 이름은 쓸 수 없음)')
    return n


def clean_dir(menu: str, sub) -> str:
    """대상 폴더 안의 하위 폴더 경로('기타서류/2024'). 맨 위는 빈 글자."""
    s = str(sub or "").strip().strip("/")
    if not s:
        return ""
    parts = s.split("/")
    if len(parts) > MAX_DEPTH:
        raise ArchiveError(f"폴더는 {MAX_DEPTH}단계까지만 만들 수 있습니다")
    if any(not p.strip() or p in (".", "..") or _BAD.search(p) or len(p) > 150 for p in parts):
        raise ArchiveError("폴더 경로가 올바르지 않습니다")
    if parts[0] in archive.RESERVED_SUBDIRS.get(menu, set()):
        raise ArchiveError(f"'{parts[0]}' 폴더는 다른 메뉴가 쓰는 폴더라 여기서 쓸 수 없습니다")
    return "/".join(parts)


def _base(menu: str, ref_id: int) -> tuple[dict, str, str]:
    t = target(menu, ref_id)
    if not t:
        raise ArchiveError("대상을 찾을 수 없습니다")
    rel = folder(menu, t).replace("\\", "/")
    full = os.path.normpath(os.path.join(root(), rel))
    if not _inside(root(), full):
        raise ArchiveError("허용되지 않은 경로")
    return t, rel, full


def _path(full: str, sub: str) -> str:
    p = os.path.normpath(os.path.join(full, *sub.split("/"))) if sub else full
    # 바로가기(링크)로 폴더 밖을 가리키는 경우까지 막음
    if not _inside(full, p) or not _inside(os.path.realpath(full), os.path.realpath(p)):
        raise ArchiveError("허용되지 않은 경로")
    return p


def _rename(src: str, dst: str):
    """덮어쓰지 않는 이름 바꾸기."""
    if os.path.exists(dst) and not (os.path.exists(src) and os.path.samefile(src, dst)):
        raise ArchiveError("같은 이름이 이미 있습니다")
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.replace(src, dst)
    except OSError as ex:
        raise ArchiveError(f"옮기지 못했습니다: {ex.__class__.__name__} (다른 사람이 파일을 열어 두었거나 NAS 연결이 끊겼습니다)") from ex


def _att(menu: str, ref_id: int, att_id) -> dict:
    a = query_one("SELECT * FROM attachments WHERE id=? AND menu=? AND ref_id=? AND deleted_at IS NULL", (int(att_id), menu, int(ref_id)))
    if not a:
        raise ArchiveError("파일을 찾을 수 없습니다 (새로 고침을 눌러 주세요)")
    return a


def _under(rel: str) -> list[dict]:
    """그 폴더 아래의 대장 서류 (LIKE 의 _ % 를 글자로 보게 앞부분을 잘라 비교)."""
    p = rel + "/"
    return query("SELECT * FROM attachments WHERE substr(rel_path, 1, ?)=? AND deleted_at IS NULL", (len(p), p))


def _only_mine(menu: str, ref_id: int, rows: list[dict]):
    if any(a["menu"] != menu or a["ref_id"] != int(ref_id) for a in rows):
        raise ArchiveError("다른 건의 서류가 함께 들어 있는 폴더라 폴더째 바꿀 수 없습니다. 파일을 하나씩 옮겨 주세요")


def _log(menu: str, action: str, t: dict, detail: str):
    audit("files", action, f"{MENUS[menu][1]} {t['title']}"[:120], detail[:200])
    archive.pending_clear(menu)


# ── 탐색기 동작 ────────────────────────────────────────────────────────
def mkdir(menu: str, ref_id: int, parent, name) -> str:
    parent, name = clean_dir(menu, parent), clean_name(name)
    sub = clean_dir(menu, f"{parent}/{name}" if parent else name)
    with FS_LOCK:
        archive.ensure_folder(menu, ref_id)
        t, _rel, full = _base(menu, ref_id)
        p = _path(full, sub)
        if os.path.exists(p):
            raise ArchiveError("같은 이름이 이미 있습니다")
        try:
            os.makedirs(p)
        except OSError as ex:
            raise ArchiveError(f"폴더를 만들지 못했습니다: {ex.__class__.__name__}") from ex
        _log(menu, "폴더 만듦", t, sub)
    return f"'{name}' 폴더를 만들었습니다"


def move(menu: str, ref_id: int, att_ids, dest) -> str:
    dest = clean_dir(menu, dest)
    with FS_LOCK:
        t, rel, full = _base(menu, ref_id)
        d = _path(full, dest)
        if not os.path.isdir(d):
            raise ArchiveError("옮길 폴더를 찾을 수 없습니다 (새로 고침을 눌러 주세요)")
        ok, errs = 0, []
        for aid in list(att_ids)[:200]:
            a = _att(menu, ref_id, aid)
            if (a["subdir"] or "") == dest:
                continue
            name = a["rel_path"].rsplit("/", 1)[-1]
            try:
                _move_file(a, rel + ("/" + dest if dest else "") + "/" + name, dest, a["orig_name"])
                ok += 1
            except ArchiveError as ex:
                errs.append(f"{a['orig_name']}: {ex}")
        if ok:
            _log(menu, "서류 옮김", t, f"{ok}개 → {dest or '맨 위 폴더'}")
    if errs and not ok:
        raise ArchiveError(" / ".join(errs[:3]))
    return (f"{ok}개를 옮겼습니다" if ok else "옮길 파일이 없습니다") + ((" · 옮기지 못한 파일 — " + " / ".join(errs[:3])) if errs else "")


def claim(menu: str, ref_id: int, names) -> str:
    """건 폴더 밖(위 폴더)에 넣어 둔 파일을 이 건의 폴더로 옮기고 대장에 올림."""
    with FS_LOCK:
        archive.ensure_folder(menu, ref_id)
        t, rel, full = _base(menu, ref_id)
        top, ok, errs = os.path.dirname(full), [], []
        allowed = {w["name"]: w for w in archive.loose(menu, ref_id)}      # 화면이 보여 준 목록 안의 이름만
        for name in list(names)[:100]:
            w = allowed.get(name)
            if not w:
                continue
            src, dst = os.path.normpath(os.path.join(top, name)), os.path.join(full, w["file"])
            if not _inside(os.path.realpath(top), os.path.realpath(src)):
                continue
            try:
                _rename(src, dst)
                ok.append(w["file"])
            except ArchiveError as ex:
                errs.append(f"{w['file']}: {ex}")
        n, e2 = archive.adopt(menu, ref_id, ok) if ok else (0, [])
        if ok:
            _log(menu, "파일을 이 건으로 가져옴", t, f"{len(ok)}개")
    errs += e2
    return (f"{n}개를 이 건으로 가져왔습니다" if n else "가져온 파일이 없습니다") + ((" · 가져오지 못한 파일 — " + " / ".join(errs[:3])) if errs else "")


def _move_file(a: dict, new_rel: str, sub: str, orig: str):
    src, dst = (os.path.normpath(os.path.join(root(), x)) for x in (a["rel_path"], new_rel))
    if not _inside(root(), src) or not _inside(root(), dst):
        raise ArchiveError("허용되지 않은 경로")
    if not os.path.exists(src):
        raise ArchiveError("보관함에 파일이 없습니다")
    _rename(src, dst)
    execute("UPDATE attachments SET rel_path=?, subdir=?, orig_name=? WHERE id=?", (new_rel, sub or None, orig, a["id"]))


def rename_file(menu: str, ref_id: int, att_id, name) -> str:
    with FS_LOCK:
        t, _rel, _full = _base(menu, ref_id)
        a = _att(menu, ref_id, att_id)
        ext = os.path.splitext(a["orig_name"])[1]
        name = str(name or "").strip()
        stem = name[:-len(ext)] if ext and name.lower().endswith(ext.lower()) else name      # 확장자는 바꿀 수 없음
        new = clean_name(stem, 100) + ext
        if new == a["orig_name"]:
            return "바뀐 것이 없습니다"
        d, old = a["rel_path"].rsplit("/", 1)
        m = STAMP.match(old)
        _move_file(a, f"{d}/{m.group(0) if m else ''}{new}", a["subdir"] or "", new)
        _log(menu, "서류 이름 바꿈", t, f"{a['orig_name']} → {new}")
    return f"이름을 바꿨습니다: {new}"


def rename_dir(menu: str, ref_id: int, path, name) -> str:
    sub, name = clean_dir(menu, path), clean_name(name)
    if not sub:
        raise ArchiveError("맨 위 폴더 이름은 시스템이 관리하므로 바꿀 수 없습니다")
    new = clean_dir(menu, (sub.rsplit("/", 1)[0] + "/" if "/" in sub else "") + name)
    if new == sub:
        return "바뀐 것이 없습니다"
    with FS_LOCK:
        t, rel, full = _base(menu, ref_id)
        src, dst = _path(full, sub), _path(full, new)
        if not os.path.isdir(src):
            raise ArchiveError("폴더를 찾을 수 없습니다 (새로 고침을 눌러 주세요)")
        rows = _under(f"{rel}/{sub}")
        _only_mine(menu, ref_id, rows)
        _rename(src, dst)
        with batch():
            for a in rows:
                rp = f"{rel}/{new}" + a["rel_path"][len(f"{rel}/{sub}"):]
                execute("UPDATE attachments SET rel_path=?, subdir=? WHERE id=?", (rp, rp[len(rel) + 1:].rsplit("/", 1)[0], a["id"]))
        _log(menu, "폴더 이름 바꿈", t, f"{sub} → {new}")
    return f"폴더 이름을 바꿨습니다: {name}"


def trash_dir(menu: str, ref_id: int, path) -> str:
    """폴더째 '_삭제함' 으로."""
    sub = clean_dir(menu, path)
    if not sub:
        raise ArchiveError("맨 위 폴더는 지울 수 없습니다")
    with FS_LOCK:
        t, rel, full = _base(menu, ref_id)
        src = _path(full, sub)
        if not os.path.isdir(src):
            raise ArchiveError("폴더를 찾을 수 없습니다 (새로 고침을 눌러 주세요)")
        rows = _under(f"{rel}/{sub}")
        _only_mine(menu, ref_id, rows)
        _rename(src, archive.trash_path(f"{rel}/{sub}"))
        with batch():
            for a in rows:
                execute("UPDATE attachments SET deleted_at=datetime('now','localtime'), deleted_by=? WHERE id=?", (current_actor.get()[1], a["id"]))
        _log(menu, "폴더 지움(삭제함으로 옮김)", t, f"{sub} · 서류 {len(rows)}개")
    return f"'{sub.rsplit('/', 1)[-1]}' 폴더를 '_삭제함'으로 옮겼습니다 (서류 {len(rows)}개 함께)"


def trash_files(menu: str, ref_id: int, att_ids) -> str:
    n = 0
    with FS_LOCK:
        for aid in list(att_ids)[:200]:
            _att(menu, ref_id, aid)
            n += 1 if archive.delete(int(aid)) else 0
    archive.pending_clear(menu)
    return f"{n}개를 지웠습니다 (파일은 보관함의 '_삭제함' 폴더로 옮겼습니다)"


# ── NAS 에 직접 넣은 파일을 자동으로 대장에 ─────────────────────────────
_seen: dict[str, tuple] = {}                  # 경로 → (크기·시각, 처음 본 때, 거절 여부)


def auto_on() -> bool:
    return get_settings().get("archive_auto") != "0"


def auto_menu(menu: str) -> bool:
    """자동으로 올릴 수 있는 메뉴인지 (건마다 폴더가 따로 있는 메뉴)."""
    return menu in PENDING and not PENDING[menu][2]


def auto_for(menu: str, ref_id: int) -> bool:
    """이 건의 폴더에 넣은 파일이 자동으로 올라가는지 (아니면 화면에 [대장에 올리기] 단추가 보임)."""
    return auto_on() and auto_menu(menu)


def refused(menu: str, ref_id: int, waiting: list[dict]) -> set[str]:
    """자동으로 올릴 수 없는 파일 (내용이 종류와 다르거나 너무 큼)."""
    try:
        _t, _rel, full = _base(menu, ref_id)
    except ArchiveError:
        return set()
    out = set()
    for w in waiting:
        s = _seen.get(os.path.normpath(os.path.join(full, w["name"])))
        if w["size"] > MAX_MB * 1024 * 1024 or (s and s[2]):
            out.add(w["name"])
    return out


def sync(menu: str, ref_id: int, limit: int = 1000) -> int:
    """대상 폴더에 들어온 새 파일을 대장에 올림. NAS 에서 옮기거나 이름을 바꾼 파일은 내용 지문으로 알아보고 대장의 위치를 맞춤."""
    if not auto_for(menu, ref_id):
        return 0
    waiting = archive.unregistered(menu, ref_id)
    if not waiting:
        return 0
    n = 0
    with FS_LOCK:
        t, rel, full = _base(menu, ref_id)
        lost: dict[str, list] = {}
        for a in query("SELECT * FROM attachments WHERE menu=? AND ref_id=? AND deleted_at IS NULL", (menu, int(ref_id))):
            if not os.path.exists(os.path.normpath(os.path.join(root(), a["rel_path"]))):
                lost.setdefault(a["sha256"], []).append(a)
        for w in waiting:
            if n >= limit:
                break
            path = os.path.normpath(os.path.join(full, w["name"]))
            try:
                if not _inside(os.path.realpath(full), os.path.realpath(path)):
                    continue
                st = os.stat(path)
                sig = (st.st_size, st.st_mtime_ns)
                old = _seen.get(path)
                if not old or old[0] != sig:                  # 처음 봤거나 바뀌는 중(복사 중): 다음에 다시 봄
                    if len(_seen) > 20000:
                        _seen.clear()
                    _seen[path] = (sig, time.monotonic(), False)
                    continue
                if old[2] or time.monotonic() - old[1] < AUTO_SETTLE:
                    continue
                with open(path, "rb") as f:
                    raw = f.read(MAX_MB * 1024 * 1024 + 1)
                try:
                    _ext, mime = check(w["file"], raw, nas=True)
                except ArchiveError:
                    _seen[path] = (sig, old[1], True)         # 종류가 맞지 않는 파일: 바뀔 때까지 다시 읽지 않음
                    continue
                if len(raw) != st.st_size:
                    continue
            except OSError:
                continue
            sha = hashlib.sha256(raw).hexdigest()
            # 같은 파일을 다른 요청이 그 사이 올렸으면 건너뜀
            if query_one("SELECT 1 FROM attachments WHERE rel_path=? AND deleted_at IS NULL", (rel + "/" + w["name"],)):
                continue
            if lost.get(sha):
                a = lost[sha].pop()
                shown = a["orig_name"] if STAMP.match(w["file"]) else w["file"][:120]
                execute("UPDATE attachments SET rel_path=?, subdir=?, orig_name=? WHERE id=?", (rel + "/" + w["name"], w["sub"] or None, shown, a["id"]))
                audit("files", "NAS에서 옮긴 서류 위치 맞춤", f"{MENUS[menu][1]} {t['title']}"[:120], w["name"][:150])
            else:
                execute("INSERT INTO attachments(menu, ref_id, company, rel_path, orig_name, mime, size, sha256, note, uploaded_by, subdir, mtime) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (menu, int(ref_id), company.norm(t.get("company")), rel + "/" + w["name"], w["file"][:120], mime, len(raw), sha,
                         None, AUTO_BY, w["sub"] or None, str(st.st_mtime_ns)))
                audit("files", "NAS 파일 자동으로 대장에 올림", f"{MENUS[menu][1]} {t['title']}"[:120], w["name"][:150])
            n += 1
    if n:
        archive.pending_clear(menu)
    return n


# ── NAS 에서 고쳐 저장한 파일을 대장에 반영 ─────────────────────────────
# 받아들이는 것: 종류(확장자)와 내용이 맞는 파일. 막는 것: 내용이 종류와 다른 파일(암호화된 파일 등), 짧은 시간의 대량 변경(랜섬웨어 의심).
# BMS 가 만든 계약서·증명서 PDF 는 이 대장이 아니라 따로 관리하므로(storage.read_verified) 고치면 여전히 열리지 않음.
BULK_LIMIT, BULK_SEC = 30, 600                # 10분에 30개를 넘게 바뀌면 멈춤
_changes: deque = deque()


def held() -> str:
    """대량 변경이 의심되어 자동 반영을 멈춘 때. 멈추지 않았으면 빈 글자."""
    return get_settings().get("archive_hold") or ""


def release():
    _changes.clear()
    set_setting("archive_hold", "")


def _apply_edit(a: dict, path: str, manual: bool) -> str:
    with FS_LOCK:
        return _apply_edit_locked(a, path, manual)


def _apply_edit_locked(a: dict, path: str, manual: bool) -> str:
    """바뀐 파일 하나를 반영. ''=아직(복사·저장 중), 'same'=내용은 같음, 'ok'=반영, 'bad'=받아들일 수 없음, 'hold'=대량 변경으로 멈춤."""
    try:
        st = os.stat(path)
        sig = (st.st_size, st.st_mtime_ns)
        if not manual:
            old = _seen.get(path)
            if not old or old[0] != sig:
                if len(_seen) > 20000:
                    _seen.clear()
                _seen[path] = (sig, time.monotonic(), False)
                return ""
            if old[2]:
                return "bad"
            if time.monotonic() - old[1] < AUTO_SETTLE:
                return ""
        with open(path, "rb") as f:
            raw = f.read(MAX_MB * 1024 * 1024 + 1)
        if len(raw) != st.st_size and st.st_size <= MAX_MB * 1024 * 1024:
            return ""
    except OSError:                                          # 다른 사람이 열어 두고 저장하는 중
        return ""
    sha = hashlib.sha256(raw).hexdigest()
    now_row = query_one("SELECT sha256 FROM attachments WHERE id=? AND deleted_at IS NULL", (a["id"],))
    if not now_row:
        return "same"
    if sha == now_row["sha256"]:                              # 그대로이거나, 같은 순간 다른 요청이 먼저 반영함 (기록을 두 번 남기지 않게)
        execute("UPDATE attachments SET mtime=? WHERE id=?", (str(st.st_mtime_ns), a["id"]))
        return "same"
    try:
        _ext, mime = check(os.path.basename(path), raw, nas=True)
    except ArchiveError:
        _seen[path] = (sig, 0.0, True)
        return "bad"
    if not manual:
        now = time.monotonic()
        while _changes and now - _changes[0] > BULK_SEC:
            _changes.popleft()
        if held() or len(_changes) >= BULK_LIMIT:
            if not held():
                set_setting("archive_hold", query_one("SELECT datetime('now','localtime') t")["t"][:16])
                audit("files", "NAS 서류 대량 변경 의심 — 자동 반영 멈춤", f"{BULK_SEC // 60}분에 {BULK_LIMIT}개 넘게 바뀜", "환경설정에서 확인 뒤 다시 켤 수 있음")
                log.warning("NAS 서류가 짧은 시간에 너무 많이 바뀌어 자동 반영을 멈춤")
            return "hold"
        _changes.append(now)
    execute("UPDATE attachments SET sha256=?, size=?, mime=?, mtime=?, modified_at=datetime('now','localtime') WHERE id=? AND sha256=?",
            (sha, len(raw), mime, str(st.st_mtime_ns), a["id"], now_row["sha256"]))
    t = target(a["menu"], a["ref_id"]) or {"title": ""}
    audit("files", "NAS에서 고친 서류 반영", f"{MENUS[a['menu']][1]} {t['title']}"[:120], f"{a['orig_name']} · {a['size'] // 1024}KB → {len(raw) // 1024}KB")
    return "ok"


def changed_path(a: dict) -> str | None:
    """대장과 달라진(달라졌을 수 있는) 파일이면 그 경로. 파일이 없거나 그대로면 None."""
    p = os.path.normpath(os.path.join(root(), a["rel_path"]))
    if not _inside(root(), p):
        return None
    try:
        st = os.stat(p)
    except OSError:
        return None
    return p if st.st_size != a["size"] or str(st.st_mtime_ns) != (a.get("mtime") or "") else None


def sync_edits(rows: list[dict]) -> int:
    """NAS 에서 고쳐 저장한 서류를 반영. 반영한 개수."""
    n = 0
    if not auto_on():
        return 0
    for a in rows:
        p = changed_path(a)
        if p and _apply_edit(a, p, False) == "ok":
            n += 1
    return n


def accept_edit(menu: str, ref_id: int, att_id) -> str:
    """사람이 직접 '고친 내용 반영'을 누름 (자동 반영을 껐거나 멈췄을 때)."""
    a = _att(menu, ref_id, att_id)
    p = changed_path(a)
    r = _apply_edit(a, p, True) if p else "same"
    if r == "bad":
        raise ArchiveError("파일 내용이 종류와 맞지 않아 반영하지 않았습니다 (파일이 깨졌거나 암호화되었을 수 있습니다)")
    if r == "":
        raise ArchiveError("파일을 읽지 못했습니다. 다른 사람이 저장하는 중일 수 있으니 잠시 뒤 다시 해 주세요")
    return "고친 내용을 반영했습니다" if r == "ok" else "바뀐 것이 없습니다"


def read_fresh(a: dict) -> bytes:
    """열기·내려받기: 방금 NAS 에서 고쳐 저장한 파일이면 반영하고 새 내용을 내줌."""
    try:
        return archive.read(a)
    except ArchiveError:
        p = changed_path(a)
        if not p or not auto_on() or held():
            raise
        try:
            s1 = os.stat(p)
            time.sleep(1.2)                                  # 저장이 끝났는지
            s2 = os.stat(p)
        except OSError:
            raise
        if (s1.st_size, s1.st_mtime_ns) != (s2.st_size, s2.st_mtime_ns):
            raise ArchiveError("지금 NAS에서 저장하는 중인 파일입니다. 잠시 뒤 다시 열어 주세요") from None
        _seen[p] = ((s2.st_size, s2.st_mtime_ns), time.monotonic() - AUTO_SETTLE, False)
        r = _apply_edit(a, p, False)
        if r == "bad":
            raise ArchiveError("NAS에서 바뀐 파일인데 내용이 종류와 맞지 않아 열지 않았습니다 (파일이 깨졌거나 암호화되었을 수 있습니다)") from None
        fresh = archive.get(a["id"])
        if r not in ("ok", "same") or not fresh:
            raise
        return archive.read(fresh)


def sync_quiet(menu: str, ref_id: int, limit: int = 20) -> int:
    try:
        sync_edits(query("SELECT * FROM attachments WHERE menu=? AND ref_id=? AND deleted_at IS NULL", (menu, int(ref_id))))
        return sync(menu, ref_id, limit)
    except Exception:                                       # noqa: BLE001  (실패해도 화면은 떠야 함)
        log.exception("자동 대장 올리기 실패: %s %s", menu, ref_id)
        return 0


def sync_all() -> int:
    if not auto_on():
        return 0
    n = sync_edits(query("SELECT * FROM attachments WHERE deleted_at IS NULL"))
    for menu, p in PENDING.items():
        if auto_menu(menu):
            for r in query(p[0]):
                n += sync_quiet(menu, r["id"], 1000)
    return n


def recent(menus) -> list[dict]:
    """최근 3일 안에 NAS 에서 자동으로 들어온 서류 (대상별)."""
    out = []
    for menu in menus:
        if menu not in PENDING or not auto_menu(menu):
            continue
        for r in query("SELECT ref_id, COUNT(*) n, MAX(uploaded_at) at, MAX(orig_name) name FROM attachments WHERE menu=? AND uploaded_by=? "
                       "AND deleted_at IS NULL AND uploaded_at>=datetime('now','localtime','-3 days') GROUP BY ref_id ORDER BY at DESC LIMIT 15",
                       (menu, AUTO_BY)):
            t = target(menu, r["ref_id"])
            if t:
                url = PENDING[menu][1] % r["ref_id"]
                out.append({"menu": menu, "ref_id": r["ref_id"], "title": t["title"], "n": r["n"], "at": (r["at"] or "")[5:16], "name": r["name"],
                            "company": company.norm(t.get("company")), "url": url + ("" if url.startswith("/files/") else "#files")})
    return company.keep_co(out)


def lost_ids(menu: str, ref_id: int, rows: list[dict]) -> set[int]:
    """대장에는 있는데 보관함에 파일이 없는 것 (NAS 에서 지우거나 다른 곳으로 옮긴 것). 보관함이 안 보일 때는 판단하지 않음."""
    try:
        _t, _rel, full = _base(menu, ref_id)
        if not os.path.isdir(full):
            return set()
        return {r["id"] for r in rows if not os.path.exists(os.path.normpath(os.path.join(root(), r["rel_path"])))}
    except (ArchiveError, OSError):
        return set()


def thumb(att: dict) -> bytes:
    """사진을 작은 그림(JPEG)으로."""
    import io

    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(archive.read(att))) as im:
        im.draft("RGB", (400, 400))
        im = ImageOps.exif_transpose(im)
        im.thumbnail((220, 220))
        out = io.BytesIO()
        im.convert("RGB").save(out, "JPEG", quality=78)
    return out.getvalue()


def tiff_pdf(att: dict) -> bytes:
    """TIFF(여러 장 스캔본 포함)를 브라우저에서 볼 수 있게 PDF 로 (50장까지). 원본은 그대로."""
    import io

    from PIL import Image, ImageSequence
    with Image.open(io.BytesIO(read_fresh(att))) as im:
        pages = []
        for n, f in enumerate(ImageSequence.Iterator(im)):
            if n >= 50:
                break
            p = f.convert("RGB")
            p.thumbnail((2400, 2400))
            pages.append(p)
    out = io.BytesIO()
    pages[0].save(out, "PDF", save_all=True, append_images=pages[1:])
    return out.getvalue()
