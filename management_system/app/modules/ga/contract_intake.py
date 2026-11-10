"""계약서 폴더 자동 감지 → 계약·임대차 등록 초안.

  {contract_root}/2026 본사 임대차/임대차계약서.pdf ...
      새 폴더 발견 → 계약서에서 상대방·기간·금액 등을 읽음 → ga_contract_drafts 저장 → 계약·임대차 화면에 '확인필요'
      사람이 확인하고 [등록]해야 계약 목록에 들어갑니다.

임직원 서류 감지(app/modules/hr/intake.py)와 같은 방식이며, 파일 읽기(PDF 글자층·OCR·hwp)도 그것을 그대로 씁니다.
폴더는 읽기만 하고 고치지 않습니다. 원문은 저장하지 않고 읽어 낸 항목만 암호화해 저장합니다.
"""
import logging
import os
import re
import threading
from datetime import datetime

from app.config import settings
from app.db import audit, execute, get_settings, query, query_one
from app.modules.hr import intake
from app.security.crypto import enc

log = logging.getLogger("bms")

state = {"last_scan": "", "error": "", "enabled": False}
_lock = threading.Lock()
_stop = threading.Event()
_thread: threading.Thread | None = None
_seen: dict[str, str] = {}

_DATE = r"((?:19|20)\d{2})\s*[년.\-/]\s*(\d{1,2})\s*[월.\-/]\s*(\d{1,2})\s*일?"
_NUM = r"(\d{1,3}(?:,\d{3})+|\d{4,})"
CATEGORY_RULES = [  # (찾을 말, 구분) 앞에서부터 먼저 맞는 것
    (r"임\s*대\s*차|임\s*대\s*료|전\s*세|월\s*세|차\s*임", "임대차"),
    (r"리\s*스|렌\s*탈|렌\s*트|대\s*여", "리스·렌탈"),
    (r"유\s*지\s*보\s*수|유\s*지\s*관\s*리", "유지보수"),
    (r"보\s*험", "보험"),
    (r"통\s*신|인\s*터\s*넷|회\s*선|클\s*라\s*우\s*드|소\s*프\s*트\s*웨\s*어|라\s*이\s*선\s*스|호\s*스\s*팅", "통신·IT서비스"),
    (r"용\s*역|위\s*탁|컨\s*설\s*팅", "용역"),
    (r"구\s*매|매\s*매|공\s*급|납\s*품", "구매"),
]
PARTY_LABELS = r"임\s*대\s*인|임\s*차\s*인|갑|을|발\s*주\s*자|수\s*급\s*인|수\s*급\s*자|공\s*급\s*자|수\s*요\s*자|계약\s*상대자|상\s*호|회\s*사\s*명|업\s*체\s*명|법\s*인\s*명"


# ════════════════════════════════════════════════════════════════════════
# 글 → 항목
# ════════════════════════════════════════════════════════════════════════
def _iso(y, m, d) -> str | None:
    try:
        return datetime(int(y), int(m), int(d)).date().isoformat()
    except ValueError:
        return None


def _int(s: str) -> int:
    return int(re.sub(r"\D", "", s))


def _core(name: str) -> str:
    return re.sub(r"\(\s*주\s*\)|㈜|주식회사|\s", "", name or "")


def _clean_party(v: str) -> str:
    v = re.split(r"\s*(?:\(\s*인\s*\)|\(\s*서명|대\s*표\s*(?:이\s*사|자)?\s*[:：]?|주\s*소\s*[:：]|사업자|전\s*화|연\s*락\s*처|$)", v)[0]
    v = v.strip(" :：,·").rstrip("(").strip()
    if v.endswith(")") and v.count("(") < v.count(")"):      # 짝 없는 닫는 괄호만 정리 ('(주)○○' 는 그대로)
        v = v[:-1].strip()
    return v


def extract(folder_name: str, pages: list, company_name: str = "") -> dict:
    """계약서 글에서 등록에 필요한 항목을 읽습니다. 못 찾은 항목은 checks 에 남깁니다."""
    text = "\n".join(p.text for p in pages)
    flat = re.sub(r"[ \t　]+", " ", text)
    f, src, checks = {}, {}, []

    f["title"] = re.sub(r"\s+", " ", folder_name).strip()
    hay = f["title"] + "\n" + "\n".join(flat.splitlines()[:5])
    f["category"] = next((cat for pat, cat in CATEGORY_RULES if re.search(pat, hay)), None) or \
        next((cat for pat, cat in CATEGORY_RULES if re.search(pat, flat)), "기타")

    # 상대방: '임대인 : ○○', '(갑) 상호 : ○○' 중 우리 회사가 아닌 쪽
    ours = _core(company_name)
    for m in re.finditer(rf"(?:{PARTY_LABELS})\s*[\(（]?\s*[갑을]?\s*[\)）]?\s*(?:상\s*호|성\s*명|회\s*사\s*명)?\s*[:：]\s*([^\n]{{2,40}})", flat):
        cand = _clean_party(m.group(1))
        if len(cand) >= 2 and not (ours and ours in _core(cand)) and not re.fullmatch(r"[\d\-.,\s]+", cand):
            f["counterparty"] = cand
            break
    if not f.get("counterparty"):
        for m in re.finditer(r"(?:\(\s*주\s*\)|㈜|주식회사)\s*[가-힣A-Za-z0-9&]{2,20}|[가-힣A-Za-z0-9&]{2,20}\s*(?:\(\s*주\s*\)|주식회사)", flat):
            if not (ours and ours in _core(m.group(0))):
                f["counterparty"] = m.group(0).strip()
                break

    # 기간: 'A 부터 B 까지' / 'A ~ B'
    m = re.search(rf"{_DATE}\s*(?:부터|~|∼|-|–|에서)\s*{_DATE}", flat)
    if m:
        f["start_date"], f["end_date"] = _iso(*m.group(1, 2, 3)), _iso(*m.group(4, 5, 6))
    else:
        m = re.search(rf"(?:계약\s*기간|임대차\s*기간|계약\s*개시일|시작일)[^\n]{{0,15}}?{_DATE}", flat)
        if m:
            f["start_date"] = _iso(*m.group(1, 2, 3))

    # 금액: 월 차임·사용료 → 월, 없으면 계약금액·총액 → 일시(연간이면 연)
    m = re.search(rf"(?:월\s*(?:차\s*임|세|임\s*대\s*료|사\s*용\s*료|이\s*용\s*료|요\s*금|렌\s*탈\s*료|리\s*스\s*료|대\s*여\s*료|관\s*리\s*비))[^\n\d]{{0,40}}?[₩￦\\]?\s*{_NUM}", flat)
    if m:
        f["amount"], f["pay_cycle"] = _int(m.group(1)), "월"
    else:
        m = re.search(rf"(연\s*간|년\s*간)?\s*(?:계\s*약\s*(?:금\s*액|대\s*금)|총\s*액|용\s*역\s*(?:대\s*금|비)|보\s*험\s*료|계약\s*총액)[^\n\d]{{0,40}}?[₩￦\\]?\s*{_NUM}", flat)
        if m:
            f["amount"], f["pay_cycle"] = _int(m.group(2)), ("연" if m.group(1) else "일시")
    m = re.search(rf"보\s*증\s*금[^\n\d]{{0,40}}?[₩￦\\]?\s*{_NUM}", flat)
    if m:
        f["deposit"] = _int(m.group(1))

    m = re.search(r"(?:부\s*가\s*(?:가\s*치\s*)?세|V\s*A\s*T)\s*[\(（]?\s*(별\s*도|포\s*함|미\s*포\s*함)", flat, re.I)
    if m:
        f["amount_vat"] = "부가세포함" if "포" in m.group(1) and "미" not in m.group(1) else "부가세별도"
    elif re.search(r"면\s*세", flat):
        f["amount_vat"] = "면세"

    if re.search(r"자\s*동\s*(?:으\s*로\s*)?(?:갱\s*신|연\s*장)|묵\s*시\s*적\s*(?:으\s*로\s*)?(?:갱\s*신|연\s*장)|동\s*일\s*한\s*조\s*건\s*으\s*로\s*(?:갱\s*신|연\s*장)", flat):
        f["auto_renew"] = 1
    m = re.search(r"만\s*료\s*(?:일\s*)?(?:로\s*부\s*터\s*)?(\d{1,3})\s*(개\s*월|일)\s*전", flat)
    if m:
        f["notice_days"] = int(m.group(1)) * (30 if "개" in m.group(2) else 1)
    m = re.search(r"계\s*약\s*번\s*호\s*[:：]\s*([A-Za-z0-9\-_/]{3,30})", flat)
    if m:
        f["contract_no"] = m.group(1)

    for key, label in (("counterparty", "상대방"), ("start_date", "계약 시작일"), ("end_date", "계약 종료일"), ("amount", "금액")):
        if not f.get(key):
            checks.append(f"{label}을(를) 서류에서 찾지 못했습니다")
    if pages and any(p.ocr for p in pages):
        checks.append("스캔본을 판독했습니다. 숫자·이름을 원본과 대조하세요")
    if not pages:
        checks.append("읽을 수 있는 서류가 없습니다 (PDF·스캔 이미지·한글 파일을 넣어 주세요)")
    return {"fields": {k: v for k, v in f.items() if v not in (None, "")}, "checks": checks}


# ════════════════════════════════════════════════════════════════════════
# 초안 저장소
# ════════════════════════════════════════════════════════════════════════
def list_drafts(status: str = "") -> list[dict]:
    sql, p = "SELECT * FROM ga_contract_drafts", []
    if status:
        sql += " WHERE status=?"
        p.append(status)
    return query(sql + " ORDER BY detected_at DESC, id DESC", p)


def get_draft(did: int) -> dict | None:
    return query_one("SELECT * FROM ga_contract_drafts WHERE id=?", (did,))


def update_draft(did: int, **kw):
    if "data" in kw:
        kw["data"] = enc(kw["data"])
    sets = ", ".join(f"{k}=?" for k in kw) + ", updated_at=datetime('now','localtime')"
    execute(f"UPDATE ga_contract_drafts SET {sets} WHERE id=?", list(kw.values()) + [did])


def _contract_folders() -> dict[str, int]:
    return {r["folder_name"]: r["id"] for r in query("SELECT id, folder_name FROM ga_contracts WHERE folder_name IS NOT NULL AND folder_name != ''")}


def folder_path(folder_name: str) -> str | None:
    root = settings.contract_root
    if not root or not folder_name:
        return None
    path = os.path.realpath(os.path.join(root, folder_name))
    return path if os.path.dirname(path) == os.path.realpath(root) and os.path.isdir(path) else None


def list_files(folder_name: str) -> list[str]:
    path = folder_path(folder_name)
    if not path:
        return []
    try:
        return [f.name for f in intake._employee_files(path)]
    except OSError:
        return []


# ════════════════════════════════════════════════════════════════════════
# 감지
# ════════════════════════════════════════════════════════════════════════
def build(folder: str, folder_name: str) -> dict:
    pages, files = intake.read_folder(folder)
    data = extract(folder_name, pages, get_settings().get("company_name", ""))
    data["files"] = files
    return data


def scan_once(force: bool = False) -> dict:
    root = settings.contract_root
    if not root:
        return {"ok": False, "message": "계약서 폴더가 설정되지 않았습니다", "pending": False}
    if not _lock.acquire(blocking=False):
        return {"ok": True, "message": "폴더를 확인하는 중입니다", "pending": False}
    created = updated = 0
    pending = False
    try:
        try:
            with os.scandir(root) as it:
                folders = [e for e in it if e.is_dir(follow_symlinks=False) and not e.name.startswith((".", "@", "#"))]
        except OSError as ex:
            state["error"] = f"계약서 폴더에 접근할 수 없습니다: {ex.__class__.__name__}"
            log.warning("contract_root 접근 실패: %s", ex)
            return {"ok": False, "message": state["error"], "pending": False}
        state["error"] = ""
        drafts = {d["folder_name"]: d for d in list_drafts()}
        linked = _contract_folders()
        for entry in folders:
            name, draft = entry.name, drafts.get(entry.name)
            if name in linked:                                   # 이미 등록된 계약 폴더
                if draft and draft["status"] == "확인필요":
                    update_draft(draft["id"], status="등록완료", contract_id=linked[name])
                continue
            if draft and draft["status"] in ("등록완료", "제외"):
                continue
            try:
                sig = intake.signature(entry.path)
                if not intake._employee_files(entry.path) or (draft and draft["signature"] == sig):
                    continue
                if not force and _seen.get(name) != sig:           # 복사 중일 수 있으므로 다음 확인 때 처리
                    _seen[name] = sig
                    pending = True
                    continue
                data = build(entry.path, name)
            except OSError as ex:
                log.warning("계약서 폴더 읽기 실패 %s: %s", name, ex)
                continue
            if draft:
                created += draft["status"] == "삭제"
                updated += draft["status"] != "삭제"
                update_draft(draft["id"], signature=sig, data=data, status="확인필요")
            else:
                execute("INSERT INTO ga_contract_drafts(folder_name, signature, data) VALUES (?,?,?)", (name, sig, enc(data)))
                created += 1
                audit("ga", "계약 초안 생성", name, f"서류 {len(data['files'])}건")
        names = {e.name for e in folders}
        for name, draft in drafts.items():
            if name not in names and draft["status"] in ("확인필요", "삭제"):
                execute("DELETE FROM ga_contract_drafts WHERE id=?", (draft["id"],))
        state["last_scan"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return {"ok": True, "pending": pending, "message": f"계약서 폴더 확인 완료: 새 초안 {created}건, 갱신 {updated}건"}
    finally:
        _lock.release()


def _loop():
    while not _stop.is_set():
        pending = False
        try:
            pending = scan_once()["pending"]
        except Exception:  # 일시 오류로 감지가 멈추지 않도록 다음 주기에 재시도
            log.exception("계약서 폴더 확인 오류")
        _stop.wait(min(10, settings.intake_interval_sec) if pending else settings.intake_interval_sec)


def start_worker():
    global _thread
    if not settings.contract_root or settings.intake_interval_sec <= 0 or _thread:
        return
    state["enabled"] = True
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="bms-contracts", daemon=True)
    _thread.start()


def stop_worker():
    global _thread
    _stop.set()
    _thread = None
