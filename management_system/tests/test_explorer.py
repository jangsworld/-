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
    real, archive.start_worker = archive.start_worker, lambda: None      # 뒤에서 도는 정리 작업이 시험 도중 끼어들지 않게
    try:
        yield from _client()
    finally:
        archive.start_worker = real


def _client():
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
    assert _up(client, i, "속지.log", b"x")["ok"] is False
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
    _write(os.path.join(base, "변경계약", "메모.log"), b"memo")
    assert _op(client, i, op="rename_dir", dir="변경계약", name="변경_계약%").json()["ok"]
    assert {r["subdir"] for r in _items(i).values()} == {"변경_계약%", "변경_계약%/2026년"} and all(archive.read(r) == PDF for r in _items(i).values())
    assert os.path.exists(os.path.join(base, "변경_계약%", "메모.log")) and archive.rehome() == 0
    # 폴더 지우기: 폴더째 _삭제함 (대장에 없는 파일도 함께), 같은 이름이 또 있어도 덮어쓰지 않음
    trash = os.path.join(archive.root(), archive.TRASH, os.path.relpath(base, archive.root()))
    assert _op(client, i, op="trash_dir", dir="변경_계약%").json()["ok"]
    assert not os.path.exists(os.path.join(base, "변경_계약%")) and os.path.exists(os.path.join(trash, "변경_계약%", "메모.log")) and _items(i) == {}
    _op(client, i, op="mkdir", dir="", name="변경_계약%")
    assert _op(client, i, op="trash_dir", dir="변경_계약%").json()["ok"]
    assert len([d for d in os.listdir(trash) if d.startswith("변경_계약%")]) == 2 and os.path.exists(os.path.join(trash, "변경_계약%", "메모.log"))
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
    """다른 건의 서류가 섞여 들어 있는 폴더는 폴더째 바꾸거나 지울 수 없음."""
    i, j = cid("보호 하나"), cid("보호 둘")
    base = _base(i)
    os.makedirs(os.path.join(base, "영수증"))
    _up(client, i, "하나.pdf", d="영수증")
    a = _items(i)["하나.pdf"]
    _write(os.path.join(base, "영수증", "남의것.pdf"))
    execute("INSERT INTO attachments(menu, ref_id, company, rel_path, orig_name, mime, size, sha256) VALUES (?,?,?,?,?,?,?,?)",
            ("ga.contracts", j, "1", a["rel_path"].rsplit("/", 1)[0] + "/남의것.pdf", "남의것.pdf", "application/pdf", len(PDF), a["sha256"]))
    for b in ({"op": "trash_dir", "dir": "영수증"}, {"op": "rename_dir", "dir": "영수증", "name": "x"}):
        r = _op(client, i, **b).json()
        assert r["ok"] is False and "다른 건의 서류" in r["msg"]
    vid = ga.save("ga_vehicles", None, {"plate_no": "99자9999", "model": "예약차", "status": "운행", "company": "1"})
    try:
        assert client.post(f"/files/ga.vehicles/{vid}/op", json={"op": "mkdir", "dir": "", "name": "과태료"}).json()["ok"] is False
    finally:
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


def _mk(title, start, end, **kw):
    return ga.save("ga_contracts", None, {"category": "렌탈", "title": title, "counterparty": "상대", "status": "유효", "company": "1",
                                          "start_date": start, "end_date": end, **kw})


def _clean(*ids):
    for i in ids:
        execute("DELETE FROM attachments WHERE menu='ga.contracts' AND ref_id=?", (i,))
        execute("DELETE FROM archive_folders WHERE menu='ga.contracts' AND ref_id=?", (i,))
        execute("DELETE FROM ga_contracts WHERE id=?", (i,))


def test_same_title_gets_own_folder(client, clock):
    """이름이 같은 계약을 새로 등록하면 폴더가 따로 생기고(기간을 붙임), 먼저 있던 건의 폴더도 파일째 그대로 이름만 바뀜. 자동 등록도 됨."""
    i = _mk("같은이름 복합기", "2025-02-25", "2028-08-25")
    j = k = None
    try:
        base = _base(i)
        assert os.path.basename(base) == "같은이름 복합기"                       # 하나뿐이면 이름 그대로
        _up(client, i, "첫계약.pdf", PDF + b"1")
        os.makedirs(os.path.join(base, "하위"))
        _write(os.path.join(base, "하위", "대장에 없는 것.log"), b"memo")
        r = client.post("/ga/contracts/save", data={"category": "렌탈", "title": "같은이름 복합기", "counterparty": "상대", "status": "유효", "company": "1",
                                                    "start_date": "2025-03-27", "end_date": "2028-09-27"}, follow_redirects=False)
        from urllib.parse import unquote
        assert "따로 만들었습니다" in unquote(r.headers["location"])
        j = max(x["id"] for x in query("SELECT id FROM ga_contracts WHERE title='같은이름 복합기'"))
        bi, bj = _base(i), _base(j)
        assert os.path.basename(bi) == "같은이름 복합기 (25.02.25~28.08.25)" and os.path.basename(bj) == "같은이름 복합기 (25.03.27~28.09.27)"
        assert not os.path.exists(base) and os.path.exists(os.path.join(bi, "하위", "대장에 없는 것.log"))   # 폴더째 따라감
        assert archive.read(_items(i)["첫계약.pdf"]) == PDF + b"1" and archive.rehome() == 0
        _up(client, j, "둘째계약.pdf", PDF + b"2")
        assert set(_items(i)) == {"첫계약.pdf"} and set(_items(j)) == {"둘째계약.pdf"}
        _write(os.path.join(bj, "NAS에서.pdf"), PDF + b"3")                       # 이제 자동 등록도 됨
        for _ in range(2):
            clock.now += 30
            archive_ops.sync("ga.contracts", i)
            archive_ops.sync("ga.contracts", j)
        assert set(_items(i)) == {"첫계약.pdf"} and set(_items(j)) == {"둘째계약.pdf", "NAS에서.pdf"}
        # 이력도 건마다 따로
        assert "둘째계약.pdf" not in client.get(f"/ga/contracts/{i}").text and "둘째계약.pdf" in client.get(f"/ga/contracts/{j}").text
        # 이름을 바꾸면 폴더째 바로 따라가고, 하나만 남은 건은 이름 그대로 돌아감
        client.post("/ga/contracts/save", data={"id": j, "category": "렌탈", "title": "둘째 복합기", "counterparty": "상대", "status": "유효",
                                                "start_date": "2025-03-27", "end_date": "2028-09-27"})
        assert os.path.basename(_base(j)) == "둘째 복합기" and os.path.exists(os.path.join(_base(j), "NAS에서.pdf")) and not os.path.exists(bj)
        assert os.path.basename(_base(i)) == "같은이름 복합기" and archive.read(_items(i)["첫계약.pdf"]) == PDF + b"1"
        # 기간까지 같으면 등록번호로 나눔
        k = _mk("같은이름 복합기", "2025-02-25", "2028-08-25")
        names = {os.path.basename(_base(i)), os.path.basename(_base(k))}
        assert len(names) == 2 and all("등록" in n for n in names)
    finally:
        _clean(*[x for x in (i, j, k) if x])


def test_legacy_shared_folder_is_split(client):
    """예전에 한 폴더에 섞인 두 건: 서류를 각자 폴더로 나누고, 아무 파일도 없어지지 않음. 다 옮겨 빈 폴더만 _빈폴더 로."""
    i, j = _mk("섞인 복합기", "2025-02-25", "2028-08-25"), _mk("섞인 복합기", "2025-03-27", "2028-09-27")
    try:
        co_dir = os.path.join(archive._safe(archive.company.short("1"), 30), "총무관리", "계약·만기 관리")
        old = os.path.join(archive.root(), co_dir, "섞인 복합기")
        os.makedirs(os.path.join(old, "하위"))
        for cid_, name, sd, raw in ((i, "일번.pdf", "", PDF + b"1"), (j, "이번.pdf", "", PDF + b"2"), (j, "이번하위.pdf", "하위", PDF + b"3")):
            _write(os.path.join(old, *([sd] if sd else []), name), raw)
            rel = "/".join(co_dir.replace("\\", "/").split("/") + ["섞인 복합기"] + ([sd] if sd else []) + [name])
            execute("INSERT INTO attachments(menu, ref_id, company, rel_path, orig_name, mime, size, sha256, subdir) VALUES (?,?,?,?,?,?,?,?,?)",
                    ("ga.contracts", cid_, "1", rel, name, "application/pdf", len(raw), __import__("hashlib").sha256(raw).hexdigest(), sd or None))
        _write(os.path.join(old, "주인 모름.pdf"), PDF + b"9")
        archive.ensure_all()
        archive.rehome()
        bi, bj = _base(i), _base(j)
        assert sorted(os.listdir(bi)) == ["일번.pdf"] and sorted(os.listdir(bj)) == ["이번.pdf", "하위"]
        assert archive.read(_items(i)["일번.pdf"]) == PDF + b"1" and archive.read(_items(j)["이번하위.pdf"]) == PDF + b"3"
        assert os.path.exists(os.path.join(old, "주인 모름.pdf"))                   # 누구 것인지 모르는 파일은 그 자리에
        os.replace(os.path.join(old, "주인 모름.pdf"), os.path.join(bi, "주인 모름.pdf"))
        assert archive.rehome() == 0
    finally:
        _clean(i, j)


def test_shared_empty_old_folder_goes_to_empty_box(client):
    i, j = _mk("빈섞임 계약", "2025-01-01", "2025-12-31"), _mk("빈섞임 계약", "2026-01-01", "2026-12-31")
    try:
        co_dir = "/".join([archive._safe(archive.company.short("1"), 30), "총무관리", "계약·만기 관리", "빈섞임 계약"])
        old = os.path.join(archive.root(), *co_dir.split("/"))
        os.makedirs(old)
        for cid_, name in ((i, "a.pdf"), (j, "b.pdf")):
            _write(os.path.join(old, name))
            execute("INSERT INTO attachments(menu, ref_id, company, rel_path, orig_name, mime, size, sha256) VALUES (?,?,?,?,?,?,?,?)",
                    ("ga.contracts", cid_, "1", co_dir + "/" + name, name, "application/pdf", len(PDF), __import__("hashlib").sha256(PDF).hexdigest()))
        client.get(f"/ga/contracts/{i}")                                          # 화면만 열어도 나뉨
        assert os.path.basename(_base(i)) == "빈섞임 계약 (25.01.01~25.12.31)" and archive.read(_items(j)["b.pdf"]) == PDF
        assert not os.path.exists(old) and os.path.isdir(os.path.join(archive.root(), archive.EMPTY, *co_dir.split("/")))
    finally:
        _clean(i, j)


def test_new_contract_never_mixes_with_deleted_one(client, clock):
    """지운 계약의 폴더가 남아 있을 때 같은 이름으로 새로 등록해도 옛 서류와 섞이지 않음. 미리 만들어 둔 폴더는 그대로 씀."""
    i = _mk("지웠다 다시", "2024-01-01", "2024-12-31")
    j = k = None
    try:
        old = _base(i)
        _write(os.path.join(old, "옛서류.pdf"))
        execute("DELETE FROM ga_contracts WHERE id=?", (i,))
        j = _mk("지웠다 다시", "2026-01-01", "2026-12-31")
        assert os.path.basename(_base(j)) == "지웠다 다시 (26.01.01~26.12.31)" and archive.unregistered("ga.contracts", j) == []
        assert os.path.exists(os.path.join(old, "옛서류.pdf"))
        pre = os.path.join(os.path.dirname(old), "미리 만든 폴더 계약")
        os.makedirs(pre)
        _write(os.path.join(pre, "미리.pdf"))
        k = _mk("미리 만든 폴더 계약", "2026-01-01", "2026-12-31")
        assert _base(k) == pre and [w["name"] for w in archive.unregistered("ga.contracts", k)] == ["미리.pdf"]
    finally:
        _clean(*[x for x in (i, j, k) if x])


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


def test_auto_folder_has_no_button(client, cid, clock):
    """자동으로 올라가는 건: [대장에 올리기] 줄·체크 칸·목록 알림이 없고 '등록 중'으로 보이다가 올라감. 자동이 안 되는 건에는 단추가 남음."""
    i = cid("탐색기 단추없음")
    base = _base(i)
    _write(os.path.join(base, "납부확인서.pdf"))
    _write(os.path.join(base, "깨진.pdf"), b"broken")
    url = f"/files/ga.contracts/{i}/panel"
    frag = client.get(url).text
    assert "고른 파일 대장에 올리기" not in frag and "fx-check" not in frag and "등록 중" in frag and 'data-wait="2"' in frag
    assert "pendingCard" not in client.get("/ga/contracts?co=all").text
    clock.now += 10
    frag = client.get(url).text
    assert "납부확인서.pdf" in _items(i) and "열 수 없는 파일" in frag and "등록 중" not in frag and 'data-wait="0"' in frag
    # 고친 때가 이름 아래(큰 아이콘)에도 보임
    a = _items(i)["납부확인서.pdf"]
    _sync(i, clock)
    _edit(a, PDF + b" v2", 1700000000)
    frag = client.get(url).text
    assert "반영 중" in frag and 'data-wait="1"' in frag
    clock.now += 10
    frag = client.get(url).text
    assert re.search(r'class="fx-when"[^>]*>고침 \d\d-\d\d \d\d:\d\d', frag) and "반영 중" not in frag and 'data-wait="0"' in frag



def test_edit_reflected_once_when_opened_together(client, cid, clock):
    """고친 파일을 동시에 두 번 열어도 반영 기록은 한 번만."""
    import threading
    i = cid("탐색기 동시")
    _base(i)
    _up(client, i, "계약.pdf")
    _sync(i, clock)
    a = _items(i)["계약.pdf"]
    _edit(a, PDF + b" v2", 1700000000)
    got = []
    ts = [threading.Thread(target=lambda: got.append(archive_ops.read_fresh(dict(a)))) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert got == [PDF + b" v2"] * 4
    n = query("SELECT COUNT(*) n FROM audit_logs WHERE action='NAS에서 고친 서류 반영' AND target LIKE '%탐색기 동시%'")[0]["n"]
    assert n == 1


def test_fines_and_rewards_have_own_folders(client, clock):
    """과태료·사고·상벌: 건마다 폴더가 따로 생기고, 거기 넣은 파일은 자동으로 올라감. 날짜·종류가 같으면 등록번호를 붙임."""
    vid = ga.save("ga_vehicles", None, {"plate_no": "97차9797", "model": "건별차_홍길동", "status": "운행", "company": "1"})
    ids = {}
    try:
        for key, at, kind in (("a", "2026-10-01 10:00", "속도위반"), ("b", "2026-11-01 10:00", "주정차위반"), ("c", "2026-11-01 18:00", "주정차위반")):
            client.post("/ga/fines/save", data={"vehicle_id": vid, "violation_at": at, "kind": kind, "amount": "40000", "status": "접수"})
            ids[key] = query("SELECT MAX(id) n FROM ga_vehicle_fines WHERE vehicle_id=?", (vid,))[0]["n"]
        client.post("/ga/damages/save", data={"vehicle_id": vid, "occur_date": "2026-09-01", "kind": "파손", "part": "앞범퍼", "status": "접수"})
        did = query("SELECT MAX(id) n FROM ga_vehicle_damages WHERE vehicle_id=?", (vid,))[0]["n"]
        rel = {k: archive.folder("ga.fines", archive.target("ga.fines", v)).replace("\\", "/") for k, v in ids.items()}
        assert rel["a"].endswith("/법인차량/97차9797 건별차/과태료/2026-10-01 속도위반")
        assert rel["b"].endswith(f"/2026-11-01 주정차위반 (등록 {ids['b']}번)") and rel["c"].endswith(f"/2026-11-01 주정차위반 (등록 {ids['c']}번)")
        d = archive.folder("ga.damages", archive.target("ga.damages", did)).replace("\\", "/")
        assert d.endswith("/97차9797 건별차/사고·파손/2026-09-01 파손 앞범퍼")
        for x in list(rel.values()) + [d]:
            assert os.path.isdir(os.path.join(archive.root(), x)), x            # 등록하면 폴더가 미리 생김
        # 건 폴더에 넣은 파일은 자동으로, 그 건에만
        _write(os.path.join(archive.root(), rel["b"], "고지서.pdf"), PDF + b"b")
        for _ in range(2):
            clock.now += 30
            archive_ops.sync_all()
        assert [r["orig_name"] for r in archive.items("ga.fines", ids["b"])] == ["고지서.pdf"]
        assert archive.items("ga.fines", ids["a"]) == [] and archive.items("ga.fines", ids["c"]) == []
        # '과태료' 폴더에 바로 넣은 파일: 자동으로 올리지 않고 어느 건인지 고르게 함
        top = os.path.dirname(os.path.join(archive.root(), rel["a"]))
        _write(os.path.join(top, "영수증.pdf"), PDF + b"z")
        os.makedirs(os.path.join(top, "모음"))
        _write(os.path.join(top, "모음", "사진.pdf"), PDF + b"y")
        archive.pending_clear()
        page = client.get("/ga/vehicles?tab=fines&status=&co=all").text
        assert "pendingCard" in page and "정해지지 않은 파일" in page
        frag = client.get(f"/files/ga.fines/{ids['a']}/panel").text
        assert "고른 파일을 이 건으로" in frag and "영수증.pdf" in frag and "모음/사진.pdf" in frag and "고지서.pdf" not in frag
        client.post(f"/files/ga.fines/{ids['a']}/claim", data={"names": ["영수증.pdf", "모음/사진.pdf", "../../밖.pdf", f"2026-11-01 주정차위반 (등록 {ids['b']}번)/고지서.pdf"]})
        assert {r["orig_name"] for r in archive.items("ga.fines", ids["a"])} == {"영수증.pdf", "사진.pdf"}
        assert archive.loose("ga.fines", ids["a"]) == [] and len(archive.items("ga.fines", ids["b"])) == 1
        # 날짜·종류를 고치면 폴더째 따라감
        client.post("/ga/fines/save", data={"id": ids["a"], "vehicle_id": vid, "violation_at": "2026-10-02 10:00", "kind": "신호위반", "amount": "1", "status": "접수"})
        n = archive.folder("ga.fines", archive.target("ga.fines", ids["a"])).replace("\\", "/")
        assert n.endswith("/2026-10-02 신호위반") and not os.path.exists(os.path.join(archive.root(), rel["a"]))
        assert all(archive.read(r) for r in archive.items("ga.fines", ids["a"])) and archive.rehome() == 0
        # 차종을 고치면 차량 폴더째 (안의 과태료·사고 폴더도 함께)
        ga.save("ga_vehicles", vid, {"model": "새차종"})
        archive.ensure_all()
        assert archive.rehome() == 0
        assert all("/97차9797 새차종/" in r["rel_path"] and archive.read(r) for k in ids for r in archive.items("ga.fines", ids[k]))
        assert not os.path.exists(os.path.join(os.path.dirname(os.path.dirname(top)), "97차9797 건별차"))
        # 차량 서류 화면에는 과태료 서류가 섞이지 않음
        assert archive.unregistered("ga.vehicles", vid) == []
    finally:
        for k, i in ids.items():
            execute("DELETE FROM attachments WHERE menu='ga.fines' AND ref_id=?", (i,))
        execute("DELETE FROM archive_folders WHERE menu IN ('ga.fines','ga.damages')")
        execute("DELETE FROM ga_vehicle_fines WHERE vehicle_id=?", (vid,))
        execute("DELETE FROM ga_vehicle_damages WHERE vehicle_id=?", (vid,))
        execute("DELETE FROM ga_vehicles WHERE id=?", (vid,))
        archive.pending_clear()


def test_old_shared_fine_folder_is_split(client):
    """예전 방식('과태료' 폴더에 여러 건이 섞임)의 서류는 건 폴더로 나뉘고 잃는 것이 없음."""
    import hashlib
    vid = ga.save("ga_vehicles", None, {"plate_no": "96카9696", "model": "옛차", "status": "운행", "company": "1"})
    f1, f2 = [execute("INSERT INTO ga_vehicle_fines(vehicle_id, violation_at, kind, amount, status) VALUES (?,?,?,?,?)", (vid, d, "주정차", 1, "접수"))
              for d in ("2026-01-01 10:00", "2026-02-01 10:00")]
    try:
        top = os.path.join(archive.ensure_folder("ga.vehicles", vid), "과태료").replace("\\", "/")
        os.makedirs(os.path.join(archive.root(), top, "묶음"))
        for f, name, sd in ((f1, "하나.pdf", ""), (f2, "둘.pdf", ""), (f2, "셋.pdf", "묶음")):
            rel = "/".join([top] + ([sd] if sd else []) + [name])
            _write(os.path.join(archive.root(), rel), PDF + name.encode())
            execute("INSERT INTO attachments(menu, ref_id, company, rel_path, orig_name, mime, size, sha256, subdir) VALUES (?,?,?,?,?,?,?,?,?)",
                    ("ga.fines", f, "1", rel, name, "application/pdf", 1, hashlib.sha256(PDF + name.encode()).hexdigest(), sd or None))
        archive.ensure_all()
        archive.rehome()
        for f, names in ((f1, {"하나.pdf"}), (f2, {"둘.pdf", "셋.pdf"})):
            rows = archive.items("ga.fines", f)
            base = archive.folder("ga.fines", archive.target("ga.fines", f)).replace("\\", "/")
            assert {r["orig_name"] for r in rows} == names and all(r["rel_path"].startswith(base + "/") and archive.read(r) for r in rows)
    finally:
        execute("DELETE FROM attachments WHERE menu='ga.fines' AND ref_id IN (?,?)", (f1, f2))
        execute("DELETE FROM archive_folders WHERE menu='ga.fines'")
        execute("DELETE FROM ga_vehicle_fines WHERE vehicle_id=?", (vid,))
        execute("DELETE FROM ga_vehicles WHERE id=?", (vid,))


def test_reward_folder(client):
    """상벌: 직원 폴더 안에 날짜·포상/징계로 건마다 폴더."""
    from app.modules.hr import repo
    eid = repo.create_employee({"name": "상벌사원", "hire_date": "2026-01-02", "department": "경영지원팀", "rank": "사원", "employment_type": "정규직"})
    try:
        ids = [repo.save_reward(None, {"employee_id": eid, "kind": "포상", "category": "표창", "action_date": d}) for d in ("2026-03-01", "2026-04-01")]
        rels = [archive.ensure_folder("hr.rewards", i).replace("\\", "/") for i in ids]
        assert rels[0].endswith("/2026-03-01 포상") and rels[1].endswith("/2026-04-01 포상") and all("인사관리/상벌현황/" in x for x in rels)
        assert client.get(f"/files/hr.rewards/{ids[0]}").status_code == 403       # 총무 담당자는 상벌 서류를 볼 수 없음
        assert client.post(f"/files/hr.rewards/{ids[0]}/claim", data={"names": ["x"]}).status_code == 403
    finally:
        execute("DELETE FROM archive_folders WHERE menu='hr.rewards'")
        repo.delete_employee(eid)


def _img(fmt, n=1, color=(200, 60, 60)):
    b = io.BytesIO()
    ims = [Image.new("RGB", (400, 300), color) for _ in range(n)]
    ims[0].save(b, fmt, **({"save_all": True, "append_images": ims[1:]} if n > 1 else {}))
    return b.getvalue()


def test_more_kinds_and_sizes(client, cid, clock, monkeypatch):
    """도면·jfif·tiff·txt·큰 파일, 이름과 내용이 다른 사진, NAS 의 zip."""
    i = cid("탐색기 종류")
    base = _base(i)
    png, tif = _img("PNG"), _img("TIFF", 3)
    for name, raw in (("도면.dwg", b"AC1032" + b"0" * 50), ("사진.jfif", _jpg((50, 50))), ("스캔.tiff", tif), ("메모.txt", "한글 메모".encode()),
                      ("메모949.txt", "한글".encode("cp949")), ("이름만jpg.jpg", png)):
        assert _up(client, i, name, raw)["ok"], name
    it = _items(i)
    assert it["이름만jpg.jpg"]["mime"] == "image/png" and client.get(f"/files/get/{it['이름만jpg.jpg']['id']}").headers["content-type"] == "image/png"
    for name in ("도면.dwg", "메모.txt"):                                            # 내려받기만
        r = client.get(f"/files/get/{it[name]['id']}")
        assert r.headers["content-type"] == "application/octet-stream" and r.headers["content-disposition"].startswith("attachment")
    v = client.get(f"/files/view/{it['스캔.tiff']['id']}")                            # TIFF 는 PDF 로 바꿔 보여 줌 (원본은 그대로)
    assert v.status_code == 200 and v.content.startswith(b"%PDF") and v.headers["x-frame-options"] == "SAMEORIGIN" and v.content.count(b"/Type /Page\n") + v.content.count(b"/Type /Page ") >= 1
    assert client.get(f"/files/get/{it['스캔.tiff']['id']}").content == tif and client.get(f"/files/view/{it['도면.dwg']['id']}").status_code == 404
    assert client.get(f"/files/thumb/{it['스캔.tiff']['id']}").status_code == 200
    frag = client.get(f"/files/ga.contracts/{i}/panel").text
    assert 'data-view="tif"' in frag and "도면(DWG)" in frag
    # 막는 것: 이름만 바꾼 것
    for name, raw in (("가짜.dwg", b"MZ\x90\x00"), ("가짜.txt", b"MZ\x90\x00\x00"), ("가짜.jpg", b"MZ\x90\x00"), ("가짜.webp", b"RIFF0000WAVE"), ("가짜.tiff", b"MZ"),
                      ("묶음.zip", b"PK\x03\x04aa"), ("실행.exe", b"MZ"), ("페이지.html", b"<html>"), ("그림.svg", b"<svg/>")):
        assert _up(client, i, name, raw)["ok"] is False, name
    # zip 은 NAS 에 넣은 것만 보이고 내려받기만
    _write(os.path.join(base, "관련 서류.zip"), b"PK\x03\x04zzzz")
    for _ in range(2):
        clock.now += 30
        archive_ops.sync("ga.contracts", i)
    z = _items(i)["관련 서류.zip"]
    r = client.get(f"/files/get/{z['id']}")
    assert r.headers["content-type"] == "application/octet-stream" and r.headers["content-disposition"].startswith("attachment")
    # 크기
    monkeypatch.setattr(archive, "MAX_MB", 1)
    assert "너무 큽니다" in _up(client, i, "큰.pdf", PDF + b"0" * (1024 * 1024))["msg"]
    monkeypatch.undo()
    assert archive.MAX_MB == 100 and _up(client, i, "40메가.pdf", PDF + b"0" * (40 * 1024 * 1024))["ok"]
    # 조회 권한 없는 계정은 TIFF 보기도 못 함
    with TestClient(app) as v:
        _login(v, "ex_view")
        assert v.get(f"/files/view/{it['스캔.tiff']['id']}").status_code == 404
