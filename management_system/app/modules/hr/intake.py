"""임직원 서류 폴더 자동 감지 → 임직원 등록 초안 생성.

서버가 켜져 있는 동안 백그라운드 스레드가 settings.employee_root 를 주기적으로 확인합니다.
(NAS 공유폴더는 파일 변경 알림을 신뢰할 수 없어 주기 확인 방식을 사용)

  {employee_root}/장지원/01. 근로계약서.pdf ...
      새 폴더 발견 → 서류에서 기본사항 추출 → employee_drafts 저장 → 임직원 현황에 '확인필요' 표시

추출은 서버 안에서만 수행하며(외부 전송 없음) 스캔본은 Windows 내장 OCR 또는 Tesseract 를 사용합니다.
OCR 원문에는 세대원 주민번호·카드번호 등이 섞여 있으므로 저장하지 않고 추출한 항목만 암호화 저장합니다.
"""
import asyncio
import hashlib
import io
import logging
import os
import re
import shutil
import subprocess
import threading
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from app.common import today
from app.config import settings
from app.db import audit, get_settings
from app.modules.hr import repo, rrn as rrn_util
from app.modules.hr.constants import QUALIFICATION_KINDS

log = logging.getLogger("bms")

BMS_FILE_PREFIX = "BMS_"          # 시스템이 저장한 PDF: 감지·추출 대상에서 제외
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}
SUPPORTED_EXT = IMAGE_EXT | {".pdf", ".hwp", ".hwpx"}
MAX_FILE_MB = 30
MAX_PAGES = 12
MIN_TEXT_LAYER = 30               # 이보다 글자가 적은 PDF 페이지는 스캔본으로 보고 OCR
OCR_TIMEOUT_SEC = 120

state = {"last_scan": "", "error": "", "enabled": False}
_lock = threading.Lock()
_stop = threading.Event()
_thread: threading.Thread | None = None
_seen: dict[str, str] = {}        # 폴더별 직전 서명: 복사가 끝나 변화가 없을 때만 추출


@dataclass
class Page:
    file: str
    text: str
    ocr: bool = False


# ════════════════════════════════════════════════════════════════════════
# 파일 → 텍스트
# ════════════════════════════════════════════════════════════════════════
def _ocr_tesseract(img) -> str | None:
    """Linux 서버용 Tesseract OCR(한국어·영어). 설치되지 않았으면 None."""
    exe = shutil.which("tesseract")
    if not exe:
        return None
    png = io.BytesIO()
    img.convert("RGB").save(png, "PNG")
    # 기본 페이지 분석은 짧은 줄·표 서류를 거의 못 읽음. 단일 열 분석을 쓰고, 비면 흩어진 글자로 다시 읽음
    for psm in ("4", "11"):
        try:
            done = subprocess.run([exe, "stdin", "stdout", "-l", "kor+eng", "--psm", psm, "--dpi", "180"], input=png.getvalue(),
                                  capture_output=True, timeout=OCR_TIMEOUT_SEC)
        except (OSError, subprocess.TimeoutExpired) as ex:
            log.warning("Tesseract OCR 실패: %s", ex.__class__.__name__)
            return None
        text = done.stdout.decode("utf-8", "replace").strip() if done.returncode == 0 else ""
        if text:
            return text
    return None


def _ocr_image(img) -> str | None:
    """Windows 내장 OCR(한국어), 없으면 Tesseract. 둘 다 없으면 None."""
    try:
        from winrt.windows.globalization import Language
        from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.storage.streams import DataWriter
    except ImportError:
        return _ocr_tesseract(img)

    async def run():
        engine = OcrEngine.try_create_from_language(Language("ko"))
        if engine is None:
            return None
        rgba = img.convert("RGBA")
        limit = OcrEngine.max_image_dimension
        if max(rgba.size) > limit:
            ratio = limit / max(rgba.size)
            rgba = rgba.resize((int(rgba.width * ratio), int(rgba.height * ratio)))
        writer = DataWriter()
        writer.write_bytes(rgba.tobytes("raw", "BGRA"))
        bitmap = SoftwareBitmap.create_copy_from_buffer(writer.detach_buffer(), BitmapPixelFormat.BGRA8, rgba.width,
                                                        rgba.height, BitmapAlphaMode.PREMULTIPLIED)
        result = await engine.recognize_async(bitmap)
        return "\n".join(line.text for line in result.lines)

    return asyncio.run(run())


def _read_pdf(path: str, name: str) -> list[Page]:
    import pypdfium2 as pdfium
    pages = []
    doc = pdfium.PdfDocument(path)
    try:
        for i in range(min(len(doc), MAX_PAGES)):
            page = doc[i]
            text = page.get_textpage().get_text_bounded().strip()
            if len(text) >= MIN_TEXT_LAYER:
                pages.append(Page(name, text))
            else:
                ocr = _ocr_image(page.render(scale=2.5).to_pil())
                if ocr:
                    pages.append(Page(name, ocr, True))
    finally:
        doc.close()
    return pages


def _read_image(path: str, name: str) -> list[Page]:
    from PIL import Image
    with Image.open(path) as img:
        text = _ocr_image(img)
    return [Page(name, text, True)] if text else []


def _read_hwp(path: str, name: str) -> list[Page]:
    """HWP 는 본문 대신 미리보기 텍스트(PrvText)만 읽습니다. 같은 내용의 PDF 가 있으면 PDF 가 더 정확합니다."""
    import olefile
    with olefile.OleFileIO(path) as ole:
        if not ole.exists("PrvText"):
            return []
        text = ole.openstream("PrvText").read().decode("utf-16-le", "ignore").strip()
    return [Page(name, text)] if text else []


def _read_hwpx(path: str, name: str) -> list[Page]:
    with zipfile.ZipFile(path) as z:
        if "Preview/PrvText.txt" not in z.namelist():
            return []
        text = z.read("Preview/PrvText.txt").decode("utf-8", "ignore").strip()
    return [Page(name, text)] if text else []


def _employee_files(folder: str) -> list[os.DirEntry]:
    with os.scandir(folder) as it:
        return sorted((f for f in it if f.is_file(follow_symlinks=False) and not f.name.startswith((".", "~", BMS_FILE_PREFIX))),
                      key=lambda f: f.name)


def read_folder(folder: str) -> tuple[list[Page], list[dict]]:
    pages, files = [], []
    for f in _employee_files(folder):
        ext = os.path.splitext(f.name)[1].lower()
        info = {"name": f.name, "status": "읽음"}
        files.append(info)
        if ext not in SUPPORTED_EXT:
            info["status"] = "미지원 형식"
            continue
        if f.stat().st_size > MAX_FILE_MB * 1024 * 1024:
            info["status"] = f"{MAX_FILE_MB}MB 초과"
            continue
        try:
            reader = _read_pdf if ext == ".pdf" else _read_hwp if ext == ".hwp" else _read_hwpx if ext == ".hwpx" else _read_image
            found = reader(f.path, f.name)
        except Exception as ex:  # 손상·암호 걸린 파일이 있어도 나머지 서류는 계속 처리
            log.warning("서류 읽기 실패 %s: %s", f.name, type(ex).__name__)
            info["status"] = "읽기 실패"
            continue
        if not found:
            info["status"] = "글자 없음(OCR 불가)"
        elif any(p.ocr for p in found):
            info["status"] = "OCR"
        pages += found
    return pages, files


def employee_folder(emp: dict) -> str | None:
    if not settings.employee_root:
        return None
    return os.path.join(settings.employee_root, emp.get("folder_name") or emp["name"])


def list_files(emp: dict) -> list[dict]:
    """임직원 폴더의 파일 목록 (폴더가 없거나 접근 불가면 빈 목록)."""
    folder = employee_folder(emp)
    try:
        with os.scandir(folder) as it:
            found = [f for f in it if f.is_file(follow_symlinks=False) and not f.name.startswith((".", "~"))]
    except (OSError, TypeError):
        return []
    return [{"name": f.name, "size": f.stat().st_size, "date": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d"),
             "system": f.name.startswith(BMS_FILE_PREFIX)} for f in sorted(found, key=lambda f: f.name)]


def document_status(required: list[str], files: list[dict]) -> list[dict]:
    """필수서류 제출 여부: 파일명에 단어가 있으면 제출로 판단."""
    out = []
    for item in required:
        words = [w.strip() for w in item.split("|") if w.strip()]
        hit = next((f["name"] for f in files if any(w in f["name"] for w in words)), None)
        out.append({"label": " / ".join(words), "file": hit})
    return out


def signature(folder: str) -> str:
    h = hashlib.sha256()
    for f in _employee_files(folder):
        st = f.stat()
        h.update(f"{f.name}|{st.st_size}|{int(st.st_mtime)}\n".encode("utf-8", "ignore"))
    return h.hexdigest()


# ════════════════════════════════════════════════════════════════════════
# 텍스트 → 항목
# ════════════════════════════════════════════════════════════════════════
_DATE = r"((?:19|20)\d{2})\s*[년.\-]\s*(\d{1,2})\s*[월.\-]\s*(\d{1,2})"
_RE_DATE = re.compile(_DATE)
_RE_BIRTH = re.compile(r"생\s*년\s*월\s*일[^\d]{0,8}" + _DATE)
_RE_RRN = re.compile(r"(?<!\d)(\d{6})\s*[-–]\s*(\d{7})(?!\d)")
_RE_RRN_MASKED = re.compile(r"(?<!\d)(\d{6})\s*[-–]\s*(\d)\s*\*")
_RE_PHONE = re.compile(r"(?<!\d)(01[016789])[-.\s]?(\d{3,4})[-.\s]?(\d{4})(?!\d)")
_RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PROVINCE = "서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충청|충북|충남|전라|전북|전남|경상|경북|경남|제주"
_RE_ADDRESS = re.compile(rf"(?:현\s*주\s*소|주\s*소\s*[:：]?|(?<![가-힣])소\s*[:：])\s*[:：]?\s*((?:{_PROVINCE})[^\n]{{6,70}})")
_RE_HIRE = re.compile(r"(?:근로\s*개시일|입\s*사\s*일|근로계약기간은|근로계약\s*\(연봉적용\)\s*기간)\s*[:：은는]?\s*" + _DATE)
_RE_SALARY = re.compile(r"(?:계약\s*연봉\s*(?:총액|금액))\s*[:：]?[^\n]{0,30}?[₩\\￦]\s*([\d,]{9,})")
_RE_SALARY_DATE = re.compile(r"연봉\s*계약\s*기간\s*[:：]?\s*" + _DATE)
_RE_DEPT = re.compile(r"근\s*무\s*(?:장\s*소|부\s*서)\s*[:：]?[^\n]*?([가-힣]{2,}팀)")
_RE_DUTY = re.compile(r"업무\s*(?:의\s*)?내용\s*[:：]\s*([^\n]{2,40})")
_RE_BANK = re.compile(r"(국민|신한|우리|하나|농협|기업|SC제일|카카오뱅크|케이뱅크|토스뱅크|부산|대구|경남|광주|전북|제주|수협|신협|우체국)"
                      r"(?:은행)?[^\d\n]{0,12}(\d[\d\- ]{8,20}\d)")
_RE_SCHOOL = re.compile(r"([가-힣]{2,}(?:대학교|전문대학|고등학교))")   # '경영대학원' 같은 단과 명칭은 학교명으로 쓰지 않음
_RE_MAJOR = re.compile(r"([가-힣]{2,}(?:학과|학부))|([가-힣]{2,}학)\s*전공")
_RE_GRAD = re.compile(r"(?:학위\s*수여\s*(?:날\s*[자짜]|년월일|일)|학위수여년월일|졸업\s*(?:년월일|일자|일))\D{0,12}((?:19|20)\d{2})")
_EDU_RULES = [("박사", r"박\s*사"), ("석사", r"석\s*사"), ("전문학사", r"전\s*문\s*학\s*사"), ("학사", r"학\s*사"),
              ("고졸", r"고\s*등\s*학\s*교")]
_QUAL_RULES = {
    "정보통신공사기술자": r"정보통신\s*(?:공사\s*)?기술자",
    "전기공사기술자": r"전기\s*공사\s*기술자",
    "소프트웨어경력증명서": r"소프트웨어\s*(?:기술자|기술\s*경력|경력\s*증명|사업자)",
}
# '등록번호'는 주민등록번호·학위등록번호와 겹치므로 쓰지 않음
_RE_QUAL_NO = re.compile(r"(?:자격\s*번호|자격증\s*번호|경력수첩\s*번호|수첩\s*번호)\s*[:：]?\s*([A-Za-z0-9\-]{5,})")
_RE_GRADE = re.compile(r"(특급|고급|중급|초급)")
_RE_MAIN_CERT = re.compile(r"([가-힣]{2,12}(?:기술사|기능장|산업기사|기사))")


def _dates(text: str) -> list[str]:
    out = []
    for y, m, d in _RE_DATE.findall(text):
        try:
            out.append(datetime(int(y), int(m), int(d)).date().isoformat())
        except ValueError:
            continue
    return out


def _iso(m, start=1) -> str | None:
    try:
        return datetime(int(m.group(start)), int(m.group(start + 1)), int(m.group(start + 2))).date().isoformat()
    except ValueError:
        return None


def _doc_date(p: Page) -> str:
    """문서 작성일 추정: 오늘 이전의 가장 늦은 날짜."""
    limit = today().isoformat()
    return max((d for d in _dates(p.text) if d <= limit), default="")


def _clean_address(value: str) -> str:
    return re.split(r"\s{2,}|연\s*락|전\s*화|성\s*명", value)[0].strip(" ,.")


def extract(name: str, pages: list[Page], company: dict | None = None) -> dict:
    """서류 텍스트에서 기본사항을 추출합니다. 값이 틀릴 수 있으므로 등록은 담당자 확인을 거칩니다."""
    company = company or {}
    fields, sources, checks = {"name": name}, {}, []
    # 텍스트 PDF(정확) 우선, 같은 종류면 최신 문서 우선
    ordered = sorted(pages, key=lambda p: (p.ocr, _invert(_doc_date(p))))

    def put(key, value, page):
        if value and key not in fields:
            fields[key] = value
            sources[key] = {"file": page.file, "ocr": page.ocr}

    # 생년월일·주민번호 (등본에는 세대원 번호도 있으므로 본인 생년월일과 맞는 번호만 사용)
    births = Counter()
    birth_page = {}
    for p in ordered:
        for m in _RE_BIRTH.finditer(p.text):
            iso = _iso(m)
            if iso:
                births[iso] += 1
                birth_page.setdefault(iso, p)
        for front, code in _RE_RRN_MASKED.findall(p.text):
            iso = rrn_util.birth_from(front, code)
            if iso:
                births[iso] += 1
                birth_page.setdefault(iso, p)
    birth = births.most_common(1)[0][0] if births else None
    rrn = None
    for p in ordered:
        if not re.search(r"주\s*민\s*등\s*록", p.text):
            continue
        for front, back in _RE_RRN.findall(p.text):
            candidate = rrn_util.normalize(f"{front}-{back}")
            if not candidate:
                continue
            if birth and rrn_util.parse(candidate)["birth_date"] == birth:
                rrn = candidate
                sources["rrn"] = {"file": p.file, "ocr": p.ocr}
                break
            if not birth and not rrn:
                rrn = candidate
                sources["rrn"] = {"file": p.file, "ocr": p.ocr}
        if rrn and birth:
            break
    if rrn:
        parsed = rrn_util.parse(rrn)
        birth = birth or parsed["birth_date"]
        fields["gender"] = parsed["gender"]
        sources["gender"] = sources["rrn"]
        checks.append("주민등록번호는 등본 원본과 한 자리씩 대조하세요" if sources["rrn"]["ocr"] else "주민등록번호를 확인하세요")
    if birth:
        put("birth_date", birth, birth_page.get(birth) or ordered[0])

    # 연락처·이메일
    phones = Counter(f"{a}-{b}-{c}" for p in ordered for a, b, c in _RE_PHONE.findall(p.text))
    if phones:
        top = phones.most_common(1)[0][0]
        put("phone", top, next(p for p in ordered if top.replace("-", "") in re.sub(r"[-.\s]", "", p.text)))
    for p in ordered:
        m = _RE_EMAIL.search(p.text)
        if m:
            put("email", m.group(0), p)
            break

    # 주소: 사업주 소재지 제외
    company_addr = re.sub(r"\s", "", company.get("company_address", ""))
    for p in ordered:
        for m in _RE_ADDRESS.finditer(p.text):
            before = p.text[max(0, m.start() - 80):m.start()]
            if before.rfind("사업체명") > before.rfind("근로자") or "소재지" in before[-20:]:  # 사업주 주소 제외
                continue
            value = _clean_address(m.group(1))
            if company_addr and re.sub(r"\s", "", value)[:12] == company_addr[:12]:
                continue
            put("address", value, p)
        if "address" in fields:
            break

    # 입사일: 가장 이른 계약 시작일
    hires = [(_iso(m), p) for p in ordered for m in _RE_HIRE.finditer(p.text) if _iso(m)]
    if hires:
        first = min(hires, key=lambda x: x[0])
        put("hire_date", first[0], first[1])

    # 연봉: 연도별 계약 금액 (가장 이른 값 = 입사연봉, 나머지 = 연봉 이력)
    by_year = {}
    for p in ordered:
        for m in _RE_SALARY.finditer(p.text):
            amount = int(m.group(1).replace(",", ""))
            found = _RE_SALARY_DATE.search(p.text)
            when = (_iso(found) if found else None) or _doc_date(p)
            if when and 1_000_000 <= amount < 10_000_000_000:
                by_year.setdefault(when[:4], (when, amount, p))
    salaries = [{"year": int(y), "date": v[0], "amount": v[1], "file": v[2].file, "ocr": v[2].ocr}
                for y, v in sorted(by_year.items())]

    # 고용정보
    for p in ordered:
        m = _RE_DEPT.search(p.text)
        if m:
            put("department", m.group(1), p)
        m = _RE_DUTY.search(p.text)
        if m:
            put("job_duty", m.group(1).strip(), p)
    joined = "\n".join(p.text for p in pages)
    if "기간의 정함이 없" in joined:
        fields["employment_type"] = "정규직"
    elif "기간의 정함이 있" in joined:
        fields["employment_type"] = "계약직(기간제)"

    # 최종학력: 학위·졸업 서류 중 가장 높은 학력
    best = None
    for p in ordered:
        if not re.search(r"학\s*위|졸\s*업", p.text + p.file) or "계약" in p.file:
            continue
        for rank, (level, pat) in enumerate(_EDU_RULES):
            if re.search(pat, p.text):
                if best is None or rank < best[0]:
                    best = (rank, level, p)
                break
    if best:
        _, level, p = best
        put("edu_level", level, p)
        schools = _RE_SCHOOL.findall(p.text)
        put("edu_school", schools[0] if schools else None, p)
        m = _RE_MAJOR.search(p.text)
        if m:
            put("edu_major", m.group(1) or f"{m.group(2)}과", p)
        m = _RE_GRAD.search(p.text)
        put("edu_grad_year", m.group(1) if m else (_doc_date(p)[:4] or None), p)

    # 은행계좌: 통장사본에서만 (법인카드 번호 등 오인 방지)
    for p in ordered:
        if not re.search(r"통장|계좌", p.file):
            continue
        m = _RE_BANK.search(p.text)
        if m:
            put("bank_name", m.group(1), p)
            put("bank_account", re.sub(r"\D", "", m.group(2)), p)
            break

    # 보유자격 3종
    qualifications = []
    for kind in QUALIFICATION_KINDS:
        hit = [p for p in ordered if re.search(_QUAL_RULES[kind], p.file + "\n" + p.text)]
        if not hit:
            continue
        text = "\n".join(p.text for p in hit)
        grade, no, cert = _RE_GRADE.search(text), _RE_QUAL_NO.search(text), _RE_MAIN_CERT.search(text)
        qualifications.append({"kind": kind, "license_no": no.group(1) if no else "", "grade": grade.group(1) if grade else "",
                               "main_cert": cert.group(1) if cert else "", "file": hit[0].file})
        if not no:
            checks.append(f"{kind}: 서류에서 자격번호를 찾지 못했습니다 (수료증 번호와 혼동 주의)")

    missing = [label for key, label in (("hire_date", "입사일"), ("birth_date", "생년월일"), ("phone", "연락처"),
                                        ("address", "주소"), ("email", "이메일"), ("edu_school", "학교명"))
               if not fields.get(key)]
    if missing:
        checks.append("서류에서 찾지 못한 항목: " + ", ".join(missing))
    if not fields.get("rank"):
        checks.append("직급·직책·근무지는 서류로 확인되지 않아 직접 선택이 필요합니다")
    if fields.get("edu_school"):
        checks.append("학교명은 스캔 판독 오류가 잦으니 확인하세요")
    if fields.get("hire_date"):
        checks.append("입사일은 서류상 가장 이른 계약 시작일입니다")
    return {"fields": fields, "rrn": rrn, "qualifications": qualifications, "salaries": salaries,
            "sources": sources, "checks": checks}


def _invert(iso: str) -> str:
    """정렬용: 날짜가 늦을수록 앞에 오도록 뒤집은 문자열."""
    return iso.translate(str.maketrans("0123456789", "9876543210"))


# ════════════════════════════════════════════════════════════════════════
# 폴더 확인 (백그라운드·수동 공통)
# ════════════════════════════════════════════════════════════════════════
def person_name(folder_name: str) -> str:
    """폴더명 → 성명 ('홍길동_2', '홍길동(영업)' 처럼 동명이인을 구분한 폴더 지원)."""
    return re.split(r"[\s_\-(]", folder_name.strip(), maxsplit=1)[0] or folder_name


def build_draft(folder: str, folder_name: str) -> dict:
    pages, files = read_folder(folder)
    data = extract(person_name(folder_name), pages, get_settings())
    data["files"] = files
    if not pages:
        data["checks"].insert(0, "읽을 수 있는 서류가 없어 성명만 채워졌습니다")
    return data


def scan_once(force: bool = False) -> dict:
    """폴더를 한 번 확인합니다. force 면 복사 완료 대기 없이 바로 추출합니다."""
    root = settings.employee_root
    if not root:
        return {"ok": False, "message": "임직원 서류 폴더(employee_root)가 설정되지 않았습니다", "pending": False}
    if not _lock.acquire(blocking=False):
        return {"ok": True, "message": "폴더를 확인하는 중입니다", "pending": False}
    created = updated = 0
    pending = False
    try:
        try:
            with os.scandir(root) as it:
                folders = [e for e in it if e.is_dir(follow_symlinks=False) and not e.name.startswith((".", "@", "#"))]
        except OSError as ex:
            state["error"] = f"폴더에 접근할 수 없습니다: {ex.__class__.__name__}"
            log.warning("employee_root 접근 실패: %s", ex)
            return {"ok": False, "message": state["error"], "pending": False}
        state["error"] = ""
        drafts = {d["folder_name"]: d for d in repo.list_drafts()}
        for entry in folders:
            name = entry.name
            emp_id = repo.match_employee_folder(name)
            draft = drafts.get(name)
            if emp_id:
                if draft and draft["status"] == "확인필요":
                    repo.update_draft(draft["id"], status="등록완료", employee_id=emp_id)
                continue
            if draft and draft["status"] in ("등록완료", "제외"):
                continue
            try:
                sig = signature(entry.path)
                if not _employee_files(entry.path) or (draft and draft["signature"] == sig):
                    continue
                if not force and _seen.get(name) != sig:   # 복사 중일 수 있으므로 다음 확인 때 처리
                    _seen[name] = sig
                    pending = True
                    continue
                data = build_draft(entry.path, name)
            except OSError as ex:
                log.warning("폴더 읽기 실패 %s: %s", name, ex)
                continue
            if draft:
                repo.update_draft(draft["id"], signature=sig, data=data)
                updated += 1
            else:
                repo.create_draft(name, sig, data)
                created += 1
                audit("hr", "등록초안 생성", name, f"서류 {len(data['files'])}건")
        names = {e.name for e in folders}
        for name, draft in drafts.items():
            if name not in names and draft["status"] == "확인필요":
                repo.delete_draft(draft["id"])
        state["last_scan"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return {"ok": True, "pending": pending,
                "message": f"폴더 확인 완료: 새 초안 {created}건, 갱신 {updated}건"}
    finally:
        _lock.release()


def _loop():
    while not _stop.is_set():
        pending = False
        try:
            pending = scan_once()["pending"]
        except Exception:  # 일시 오류로 감지가 멈추지 않도록 다음 주기에 재시도
            log.exception("임직원 폴더 확인 오류")
        _stop.wait(min(10, settings.intake_interval_sec) if pending else settings.intake_interval_sec)


def start_worker():
    global _thread
    if not settings.employee_root or settings.intake_interval_sec <= 0 or _thread:
        return
    state["enabled"] = True
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="bms-intake", daemon=True)
    _thread.start()


def stop_worker():
    global _thread
    _stop.set()
    _thread = None
