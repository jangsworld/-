"""서류 탐색기: 새 폴더·옮기기·이름 바꾸기·지우기, 범위·권한·덮어쓰기 방지, NAS 파일 자동으로 대장에 올리기, 미리보기."""
import io
import os
import re

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import archive, archive_ops
from app.db import execute, get_settings, query, set_setting
from app.main import app
from app.modules.ga import repo as ga
from app.security import auth

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"
ORIGIN = {"origin": "http://testserver"}


def _login(c, user):
    c.post("/login", data={"username": user, "password": "Strong-Pass-123!"}, headers=ORIGIN)
    c.headers.update(ORIGIN)
    c.headers["x-csrf-token"] = re.search(r'name="csrf-token" content="([^"]+)"', c.get("/account").text).group(1)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        for u, r in (("ex_ga", "ga"), ("ex_view", "viewer")):
            if not auth.query_one("SELECT 1 FROM users WHERE username=?", (u,)):
                auth.create_user(u, u, r, "Strong-Pass-123!", must_change=False)
        _login(c, "ex_ga")
        yield c


def _jpg(size=(1200, 900)):
    b = io.BytesIO()
    Image.new("RGB", size, (30, 90, 160)).save(b, "JPEG")
    return b.getvalue()


@pytest.fixture()
def cid(client):
    ids = []

    def make(title="탐색기 계약"):
        i = ga.save("ga_contracts", None, {"category": "임대차", "title": title, "counterparty": "상대", "status": "유효", "company": "1"})
        ids.append(i)
        return i
    yield make
    archive.pending_clear()
    for i in ids:
        execute("DELETE FROM attachments WHERE menu='ga.contracts' AND ref_id=?", (i,))
        execute("DELETE FROM ga_contracts WHERE id=?", (i,))
    archive_ops._seen.clear()


def _base(i):
    return os.path.join(archive.root(), archive.ensure_folder("ga.contracts", i))


def _op(c, i, **b):
    return c.post(f"/files/ga.contracts/{i}/op", json=b)


def _up(c, i, name, raw=PDF, d=""):
    return c.post(f"/files/ga.contracts/{i}/upload", data={"dir": d}, files={"files": (name, raw, "application/octet-stream")},
                  headers={"x-fx": "1"}).json()


def _items(i):
    return {r["orig_name"]: r for r in archive.items("ga.contracts", i)}


def _write(path, raw=PDF):
    with open(path, "wb") as f:
        f.write(raw)


def test_folder_file_actions(client, cid):
    i = cid("탐색기 동작")
    base = _base(i)
    assert _op(client, i, op="mkdir", dir="", name="변경계약").json()["ok"] and os.path.isdir(os.path.join(base, "변경계약"))
    assert _op(client, i, op="mkdir", dir="변경계약", name="2026년").json()["ok"]
    assert "이미 있습니다" in _op(client, i, op="mkdir", dir="", name="변경계약").json()["msg"]
    assert _up(client, i, "원본.pdf")["ok"] and _up(client, i, "1차.pdf", d="변경계약")["ok"]
    assert _up(client, i, "속지.txt", b"x")["ok"] is False
    assert _items(i)["1차.pdf"]["subdir"] == "변경계약"
    # 옮기기
    a = _items(i)["원본.pdf"]
    r = _op(client, i, op="move", ids=[a["id"]], dir="변경계약/2026년").json()
    assert r["ok"] and "1개를 옮겼습니다" in r["msg"]
    b = _items(i)["원본.pdf"]
    assert b["subdir"] == "변경계약/2026년" and archive.read(b) == PDF and not os.path.exists(os.path.join(archive.root(), a["rel_path"]))
    # 파일 이름 바꾸기: 확장자는 그대로, NAS 이름 앞의 날짜·시각도 유지
    assert _op(client, i, op="rename", ids=[b["id"]], name="임대차 원본").json()["ok"]
    b = _items(i)["임대차 원본.pdf"]
    assert archive_ops.STAMP.match(b["rel_path"].rsplit("/", 1)[1]) and b["rel_path"].endswith("_임대차 원본.pdf") and archive.read(b) == PDF
    assert _op(client, i, op="rename", ids=[b["id"]], name="가짜.exe").json()["ok"] and "가짜.exe.pdf" in _items(i)
    # 폴더 이름 바꾸기: 아래 서류의 위치가 모두 따라감
    _write(os.path.join(base, "변경계약", "메모.txt"), b"memo")
    assert _op(client, i, op="rename_dir", dir="변경계약", name="변경_계약%").json()["ok"]
    assert {r["subdir"] for r in _items(i).values()} == {"변경_계약%", "변경_계약%/2026년"} and all(archive.read(r) == PDF for r in _items(i).values())
    assert os.path.exists(os.path.join(base, "변경_계약%", "메모.txt")) and archive.rehome() == 0
    # 폴더 지우기: 폴더째 _삭제함 (대장에 없는 파일도 함께), 같은 이름이 또 있어도 덮어쓰지 않음
    trash = os.path.join(archive.root(), archive.TRASH, os.path.relpath(base, archive.root()))
    assert _op(client, i, op="trash_dir", dir="변경_계약%").json()["ok"]
    assert not os.path.exists(os.path.join(base, "변경_계약%")) and os.path.exists(os.path.join(trash, "변경_계약%", "메모.txt")) and _items(i) == {}
    _op(client, i, op="mkdir", dir="", name="변경_계약%")
    assert _op(client, i, op="trash_dir", dir="변경_계약%").json()["ok"]
    assert len([d for d in os.listdir(trash) if d.startswith("변경_계약%")]) == 2 and os.path.exists(os.path.join(trash, "변경_계약%", "메모.txt"))
    acts = {r["action"] for r in query("SELECT action FROM audit_logs WHERE module='files' ORDER BY id DESC LIMIT 60")}
    assert {"폴더 만듦", "서류 옮김", "서류 이름 바꿈", "폴더 이름 바꿈", "폴더 지움(삭제함으로 옮김)"} <= acts


def test_move_and_rename_never_overwrite(client, cid):
    i = cid("탐색기 덮어쓰기")
    base = _base(i)
    os.makedirs(os.path.join(base, "안"))
    _write(os.path.join(base, "같음.pdf"), b"%PDF-A")
    _write(os.path.join(base, "안", "같음.pdf"), b"%PDF-B")
    _write(os.path.join(base, "다름.pdf"), b"%PDF-C")
    archive.adopt("ga.contracts", i, ["같음.pdf", "다름.pdf"])
    it = _items(i)
    assert _op(client, i, op="move", ids=[it["같음.pdf"]["id"]], dir="안").json()["ok"] is False
    assert _op(client, i, op="rename", ids=[it["다름.pdf"]["id"]], name="같음").json()["ok"] is False
    os.makedirs(os.path.join(base, "빈"))
    assert _op(client, i, op="rename_dir", dir="안", name="빈").json()["ok"] is False
    got = [open(os.path.join(base, *p), "rb").read() for p in (("같음.pdf",), ("안", "같음.pdf"), ("다름.pdf",))]
    assert got == [b"%PDF-A", b"%PDF-B", b"%PDF-C"]


def test_trash_never_overwrites_and_failure_keeps_record(client, cid, monkeypatch):
    i = cid("탐색기 지우기")
    base = _base(i)
    bodies = [b"%PDF-1", b"%PDF-2"]
    for body in bodies:
        _write(os.path.join(base, "같은이름.pdf"), body)
        archive.adopt("ga.contracts", i, ["같은이름.pdf"])
        assert _op(client, i, op="trash", ids=[_items(i)["같은이름.pdf"]["id"]]).json()["ok"]
    trash = os.path.join(archive.root(), archive.TRASH, os.path.relpath(base, archive.root()))
    assert sorted(open(os.path.join(trash, n), "rb").read() for n in os.listdir(trash)) == bodies
    # 삭제함으로 옮기지 못하면 대장도 그대로
    _up(client, i, "잠긴.pdf")
    real = os.replace

    def locked(a, b):
        if archive.TRASH in str(b):
            raise PermissionError()
        return real(a, b)
    monkeypatch.setattr(os, "replace", locked)
    r = _op(client, i, op="trash", ids=[_items(i)["잠긴.pdf"]["id"]]).json()
    assert r["ok"] is False and "잠긴.pdf" in _items(i)
    monkeypatch.undo()
    # 손으로 되살린 파일(지운 서류와 같은 자리)은 다시 '새 파일'로 보임
    _write(os.path.join(base, "같은이름.pdf"))
    assert "같은이름.pdf" in [w["name"] for w in archive.unregistered("ga.contracts", i)]


def test_stays_inside_and_refuses_bad_names(client, cid):
    i, j = cid("탐색기 안쪽"), cid("탐색기 남의것")
    base, other = _base(i), _base(j)
    _up(client, i, "내것.pdf")
    _up(client, j, "남의것.pdf")
    mine, theirs = _items(i)["내것.pdf"], _items(j)["남의것.pdf"]
    os.makedirs(os.path.join(base, "안"))
    for name in ("../밖", "a/b", "a\\b", "..", ".숨김", "@eaDir", "~$x", "이름?", "BMS_가짜", "점.", "", "가" * 61, "_삭제함"):
        assert _op(client, i, op="mkdir", dir="", name=name).json()["ok"] is False, name
        assert _op(client, i, op="rename_dir", dir="안", name=name).json()["ok"] is False, name
    for d in ("..", "../" + os.path.basename(other), "안/../..", "a\\..\\b", "C:/Windows", "없는폴더"):
        assert _op(client, i, op="move", ids=[mine["id"]], dir=d).json()["ok"] is False, d
        assert _op(client, i, op="trash_dir", dir=d).json()["ok"] is False, d
    for d in ("..", "a\\b", "x/../../y"):
        assert _up(client, i, "x.pdf", d=d)["ok"] is False, d
    for b in ({"op": "trash_dir", "dir": ""}, {"op": "rename_dir", "dir": "", "name": "x"}, {"op": "trash_dir", "dir": "/"}):      # 맨 위 폴더
        assert _op(client, i, **b).json()["ok"] is False
    # 다른 건의 서류 번호로는 아무것도 못 함
    for b in ({"op": "move", "ids": [theirs["id"]], "dir": "안"}, {"op": "rename", "ids": [theirs["id"]], "name": "훔침"},
              {"op": "trash", "ids": [theirs["id"]]}):
        assert _op(client, i, **b).json()["ok"] is False
    assert archive.read(_items(j)["남의것.pdf"]) == PDF and archive.read(_items(i)["내것.pdf"]) == PDF
    assert _op(client, i, op="format").status_code == 400
    assert client.post(f"/files/ga.contracts/{i}/op", content=b"[]").status_code == 400
    assert _op(client, i, op="mkdir", dir="1/2/3/4/5", name="6").json()["ok"] is False


def test_permissions_and_csrf(client, cid):
    i = cid("탐색기 권한")
    _base(i)
    _up(client, i, "a.pdf")
    aid = _items(i)["a.pdf"]["id"]
    assert client.post("/files/hr.employees/1/op", json={"op": "mkdir", "dir": "", "name": "x"}).status_code == 403     # 총무 담당자는 인사 서류 불가
    assert client.get("/files/hr.employees/1/panel").status_code == 403
    with TestClient(app) as v:
        _login(v, "ex_view")
        assert _op(v, i, op="trash", ids=[aid]).status_code == 403 and v.get(f"/files/ga.contracts/{i}/panel").status_code == 403
        assert v.get(f"/files/thumb/{aid}").status_code == 404
    with TestClient(app) as n:
        assert n.post(f"/files/ga.contracts/{i}/op", json={"op": "trash", "ids": [aid]}, follow_redirects=False).status_code in (302, 303, 401, 403)
    tok = client.headers.pop("x-csrf-token")
    try:
        assert _op(client, i, op="trash", ids=[aid]).status_code == 403
    finally:
        client.headers["x-csrf-token"] = tok
    assert "a.pdf" in _items(i)


def test_shared_folder_is_protected(client, cid):
    """과태료처럼 두 건이 폴더를 같이 쓰면, 다른 건의 서류가 든 폴더는 폴더째 바꾸거나 지울 수 없음."""
    vid = ga.save("ga_vehicles", None, {"plate_no": "99자9999", "model": "공유차", "status": "운행", "company": "1"})
    f1, f2 = [execute("INSERT INTO ga_vehicle_fines(vehicle_id, violation_at, kind, amount, status) VALUES (?,?,?,?,?)", (vid, d, "주정차", 1, "접수"))
              for d in ("2026-01-01 10:00", "2026-02-01 10:00")]
    try:
        for f, n in ((f1, "하나.pdf"), (f2, "둘.pdf")):
            archive.save("ga.fines", f, n, PDF, "", "영수증")
        for b in ({"op": "trash_dir", "dir": "영수증"}, {"op": "rename_dir", "dir": "영수증", "name": "x"}):
            r = client.post(f"/files/ga.fines/{f1}/op", json=b).json()
            assert r["ok"] is False and "다른 건의 서류" in r["msg"]
        assert archive_ops.sync("ga.fines", f1) == 0
        assert client.post(f"/files/ga.vehicles/{vid}/op", json={"op": "mkdir", "dir": "", "name": "과태료"}).json()["ok"] is False
    finally:
        execute("DELETE FROM attachments WHERE menu='ga.fines' AND ref_id IN (?,?)", (f1, f2))
        execute("DELETE FROM ga_vehicle_fines WHERE vehicle_id=?", (vid,))
        execute("DELETE FROM ga_vehicles WHERE id=?", (vid,))


class _Clock:
    """archive_ops 안에서만 시각을 바꿔 끼움."""
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


def test_auto_register(client, cid, monkeypatch):
    i = cid("탐색기 자동")
    base = _base(i)
    clock = _Clock()
    monkeypatch.setattr(archive_ops, "time", clock)
    os.makedirs(os.path.join(base, "하위"))
    _write(os.path.join(base, "계약서.pdf"))
    _write(os.path.join(base, "하위", "합의서.pdf"), PDF + b"2")
    _write(os.path.join(base, "가짜.pdf"), b"not pdf")
    assert archive_ops.sync("ga.contracts", i) == 0                       # 처음 봄
    clock.now += 5
    assert archive_ops.sync("ga.contracts", i) == 0                       # 아직 복사 중일 수 있음
    _write(os.path.join(base, "계약서.pdf"), PDF + b"more")                # 바뀌는 중 → 처음부터 다시 기다림
    clock.now += 30
    assert archive_ops.sync("ga.contracts", i) == 1 and set(_items(i)) == {"합의서.pdf"}
    clock.now += 30
    assert archive_ops.sync("ga.contracts", i) == 1
    it = _items(i)
    assert set(it) == {"합의서.pdf", "계약서.pdf"} and it["합의서.pdf"]["subdir"] == "하위" and it["계약서.pdf"]["uploaded_by"] == archive_ops.AUTO_BY
    assert [w["name"] for w in archive.unregistered("ga.contracts", i)] == ["가짜.pdf"]      # 내용이 종류와 다른 파일은 올리지 않음
    # NAS 에서 옮기고 이름을 바꿔도 같은 서류로 이어짐 (두 벌이 되지 않음)
    keep = it["합의서.pdf"]["id"]
    os.replace(os.path.join(base, "하위", "합의서.pdf"), os.path.join(base, "합의서 최종.pdf"))
    assert archive_ops.lost_ids("ga.contracts", i, archive.items("ga.contracts", i)) == {keep}
    assert "파일 없음" in client.get(f"/files/ga.contracts/{i}/panel").text
    clock.now += 30
    assert archive_ops.sync("ga.contracts", i) == 1
    it = _items(i)
    assert len(it) == 2 and it["합의서 최종.pdf"]["id"] == keep and it["합의서 최종.pdf"]["subdir"] is None
    page = client.get("/ga/contracts?co=all").text                         # 알림: 최근 자동으로 들어온 서류
    assert "recentCard" in page and "탐색기 자동" in page
    before = get_settings().get("archive_auto")                            # 끄면 자동으로 올리지 않음
    set_setting("archive_auto", "0")
    try:
        _write(os.path.join(base, "끈뒤.pdf"), PDF + b"9")
        for _ in range(3):
            clock.now += 60
            assert archive_ops.sync("ga.contracts", i) == 0 and archive_ops.sync_all() == 0
    finally:
        set_setting("archive_auto", before or "")


def test_auto_skips_same_title(client, cid, monkeypatch):
    """이름이 같은 계약이 둘이면 폴더를 같이 쓰므로 자동으로 올리지 않음."""
    i, j = cid("쌍둥이 계약"), cid("쌍둥이 계약")
    _write(os.path.join(_base(i), "x.pdf"))
    clock = _Clock()
    monkeypatch.setattr(archive_ops, "time", clock)
    for _ in range(3):
        clock.now += 60
        assert archive_ops.sync("ga.contracts", i) == 0 and archive_ops.sync("ga.contracts", j) == 0


def test_preview_and_thumb(client, cid):
    i = cid("탐색기 미리보기")
    _base(i)
    _up(client, i, "사진.jpg", _jpg())
    _up(client, i, "문서.pdf")
    it = _items(i)
    r = client.get(f"/files/get/{it['문서.pdf']['id']}")
    assert r.headers["x-frame-options"] == "SAMEORIGIN" and "frame-ancestors 'self'" in r.headers["content-security-policy"]
    assert r.headers["content-disposition"].startswith("inline")
    assert client.get("/ga/contracts").headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in client.get(f"/files/ga.contracts/{i}").headers["content-security-policy"]
    t = client.get(f"/files/thumb/{it['사진.jpg']['id']}")
    assert t.status_code == 200 and t.headers["content-type"] == "image/jpeg" and t.headers["cache-control"] == "no-store"
    assert max(Image.open(io.BytesIO(t.content)).size) <= 220
    assert client.get(f"/files/thumb/{it['문서.pdf']['id']}").status_code == 404
    frag = client.get(f"/files/ga.contracts/{i}/panel").text
    assert "<html" not in frag and "data-fx" in frag and 'data-view="pdf"' in frag and "서류 올리기" in frag


class _Clock2(_Clock):
    def sleep(self, s):
        self.now += s


@pytest.fixture()
def clock(monkeypatch):
    c = _Clock2()
    monkeypatch.setattr(archive_ops, "time", c)
    archive_ops.release()
    yield c
    archive_ops.release()


def _disk(a):
    return os.path.join(archive.root(), a["rel_path"])


def _edit(a, raw, t):
    _write(_disk(a), raw)
    os.utime(_disk(a), (t, t))


def _sync(i, clock, n=2):
    k = 0
    for _ in range(n):
        clock.now += 30
        k += archive_ops.sync_edits(archive.items("ga.contracts", i))
    return k


def test_nas_edit_is_reflected(client, cid, clock):
    """NAS 에서 고쳐 저장하면 자동으로 반영되어 새 내용이 열림. 이력과 '고침' 표시가 남음."""
    i = cid("탐색기 고침")
    _base(i)
    _up(client, i, "계약.pdf")
    _sync(i, clock)
    a = _items(i)["계약.pdf"]
    assert a["mtime"] and not a["modified_at"]
    _edit(a, PDF + b" v2", 1700000000)
    assert "고쳐짐" in client.get(f"/files/ga.contracts/{i}/panel").text            # 반영 전 표시
    assert _sync(i, clock) == 1
    a = _items(i)["계약.pdf"]
    assert a["modified_at"] and archive.read(a) == PDF + b" v2" and client.get(f"/files/get/{a['id']}").content == PDF + b" v2"
    frag = client.get(f"/files/ga.contracts/{i}/panel").text
    assert "NAS에서 고침" in frag and "고쳐짐" not in frag
    assert query("SELECT 1 FROM audit_logs WHERE action='NAS에서 고친 서류 반영' AND target LIKE '%탐색기 고침%'")
    # 저장하자마자 여는 경우: 기다리지 않고 새 내용
    _edit(a, PDF + b" v3", 1700000100)
    assert client.get(f"/files/get/{a['id']}").content == PDF + b" v3"
    # 내용은 그대로인데 시각만 바뀐 것은 '고침'으로 남기지 않음
    before = _items(i)["계약.pdf"]["modified_at"]
    os.utime(_disk(a), (1700000200, 1700000200))
    assert _sync(i, clock) == 0 and _items(i)["계약.pdf"]["modified_at"] == before


def test_nas_edit_refuses_wrong_content_and_off(client, cid, clock):
    i = cid("탐색기 고침거절")
    _base(i)
    _up(client, i, "계약.pdf")
    _sync(i, clock)
    a = _items(i)["계약.pdf"]
    _edit(a, b"ENCRYPTED!!", 1700000000)                                          # 암호화된 파일처럼 종류가 다름
    assert _sync(i, clock, 4) == 0 and client.get(f"/files/get/{a['id']}").status_code == 409
    assert _op(client, i, op="accept", ids=[a["id"]]).json()["ok"] is False and _items(i)["계약.pdf"]["sha256"] == a["sha256"]
    set_setting("archive_auto", "0")                                              # 자동 반영을 끄면: 열리지 않고, 사람이 누르면 반영
    try:
        _edit(a, PDF + b" v2", 1700000100)
        assert _sync(i, clock, 3) == 0 and client.get(f"/files/get/{a['id']}").status_code == 409
        assert _op(client, i, op="accept", ids=[a["id"]]).json()["ok"] and client.get(f"/files/get/{a['id']}").content == PDF + b" v2"
    finally:
        set_setting("archive_auto", "")
    # 시각·크기가 같은데 내용만 바뀐 파일은 여전히 열지 않음
    a = _items(i)["계약.pdf"]
    _edit(a, PDF + b" v9", 1700000100)
    assert client.get(f"/files/get/{a['id']}").status_code == 409


def test_bulk_change_stops(client, cid, clock, monkeypatch):
    """짧은 시간에 너무 많이 바뀌면(랜섬웨어 의심) 자동 반영을 멈추고 알림. 관리자가 확인하면 다시 켜짐."""
    monkeypatch.setattr(archive_ops, "BULK_LIMIT", 3)
    i = cid("탐색기 대량")
    _base(i)
    for k in range(5):
        _up(client, i, f"f{k}.pdf", PDF + b"%d" % k)
    _sync(i, clock)
    for k, a in enumerate(archive.items("ga.contracts", i)):
        _edit(a, PDF + b"changed %d" % k, 1700000000)
    assert _sync(i, clock) == 3 and archive_ops.held()
    assert _sync(i, clock, 3) == 0
    assert "자동 반영을 멈췄습니다" in client.get("/ga/contracts?co=all").text
    archive_ops.release()
    assert _sync(i, clock) == 2 and not archive_ops.held()


def test_nas_edit_after_move_and_shared(client, cid, clock):
    """과태료처럼 폴더를 같이 쓰는 메뉴도, 이미 대장에 있는 서류를 고친 것은 반영됨."""
    vid = ga.save("ga_vehicles", None, {"plate_no": "98자9898", "model": "고침차", "status": "운행", "company": "1"})
    f = execute("INSERT INTO ga_vehicle_fines(vehicle_id, violation_at, kind, amount, status) VALUES (?,?,?,?,?)", (vid, "2026-01-01 10:00", "주정차", 1, "접수"))
    try:
        archive.save("ga.fines", f, "고지서.pdf", PDF)
        a = archive.items("ga.fines", f)[0]
        for _ in range(2):
            clock.now += 30
            archive_ops.sync_edits(archive.items("ga.fines", f))
        _edit(a, PDF + b" fixed", 1700000000)
        for _ in range(2):
            clock.now += 30
            archive_ops.sync_all()
        assert archive.read(archive.items("ga.fines", f)[0]) == PDF + b" fixed"
    finally:
        execute("DELETE FROM attachments WHERE menu='ga.fines' AND ref_id=?", (f,))
        execute("DELETE FROM ga_vehicle_fines WHERE id=?", (f,))
        execute("DELETE FROM ga_vehicles WHERE id=?", (vid,))
