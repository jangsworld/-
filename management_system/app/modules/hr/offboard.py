"""퇴사 절차 안내: 퇴사 예정자에게 보내는 사전 안내문 + 인사담당자용 단계별 진행표.

- 퇴직 예정일은 여기(offboardings.last_day)에만 적는다. 인사기록카드의 퇴사일은 실제 퇴직일에 따로 입력한다
  (미리 넣으면 재직자 목록·인건비 계산이 달라지므로).
- 안내문 기본 글귀는 설정(offboard_subject / offboard_body)에 저장한 것을 쓰고, 없으면 아래 기본값.
"""
from datetime import timedelta

from app import mailer
from app.common import to_date, today
from app.db import audit, batch, current_actor, execute, get_settings, query, query_one, set_setting
from app.security.crypto import enc

# (단계 키, 단계 이름, 언제, [(항목 키, 할 일, 참고)])
STAGES = [
    ("a", "사직서 접수·결재", "퇴직 희망일 30일 전", [
        ("a1", "서면 사직서 접수", "퇴직 희망일 최소 30일 전에 서면으로 제출. 구두·메신저로만 밝힌 경우 서면 제출을 요청(3영업일 이내)"),
        ("a2", "부서장 면담, 퇴직일·인수인계 일정 협의", "대체 인력과 업무 공백을 함께 확인"),
        ("a3", "대표이사 최종 결재 → 퇴직일 확정", "결재가 끝나면 다음 단계 '사전 안내'를 바로 진행"),
    ]),
    ("b", "사전 안내", "결재 후 1영업일 이내", [
        ("b1", "퇴사 사전 안내문 발송", "오른쪽 [안내문 보내기]로 보내면 자동으로 체크됩니다"),
        ("b2", "인수자 지정, 인수인계 기한 정하기", "부서장이 지정. 기한은 퇴직일 3영업일 전을 권장"),
        ("b3", "남은 연차와 사용 일정 확인", "인수인계에 지장이 없도록 부서장과 협의. 못 쓴 연차는 연차수당으로 정산"),
        ("b4", "정산 대상 확인", "교육비 지원금 반환(의무재직기간), 미리 당겨 쓴 연차, 미정산 경비·법인카드"),
    ]),
    ("c", "인수인계·자료 정리", "퇴직일 전까지", [
        ("c1", "인수인계서 작성 → 인수자 전달 → 부서장 확인", "주요 업무, 진행 현황, 계약·고객 대응, 미완료 과제, 마감일, 파일 경로"),
        ("c2", "업무자료가 회사 저장소에 있는지 부서장이 직접 열어 확인", "저장했다는 보고만으로 끝내지 않음. 편집 원본·중간본·근거자료 포함"),
        ("c3", "개인 기기·개인 메일·외부 클라우드의 회사 자료 신고·반환", "위치와 범위를 받아 회사 원본을 확보한 뒤 삭제 안내"),
        ("c4", "퇴직연금 받을 IRP 계좌 사본 받기", "계좌가 없으면 지급이 늦어지므로 퇴직일 전에 받기"),
        ("c5", "경비·법인카드 사용분 정산", "증빙과 함께 퇴직일 전에 마감"),
    ]),
    ("d", "퇴직일(마지막 근무일)", "퇴직일 당일", [
        ("d1", "장비·출입카드·열쇠·법인카드·문서·저장매체 반납 확인", "PC·노트북은 포맷하지 않은 그대로 받기"),
        ("d2", "업무용 계정 회수·권한 이관", "메일, 그룹웨어, 저장소, 업무시스템. 시스템관리자가 처리"),
        ("d3", "비밀유지 의무(퇴직 후 3년) 다시 안내", "고객·영업·기술·재무·인사 정보"),
        ("d4", "인사기록카드에 퇴사일·퇴사사유 입력", "입력하면 재직자 목록에서 빠지고 인사발령(퇴사)이 기록됩니다"),
    ]),
    ("e", "퇴직 후 처리", "퇴직일 다음 날부터", [
        ("e1", "4대보험 자격상실 신고", "건강보험은 14일 이내, 국민연금·고용·산재는 다음 달 15일까지"),
        ("e2", "마지막 급여·연차수당 지급, 소득세·보험료 정산", "퇴직일부터 14일 이내 지급, 정산 내역 안내"),
        ("e3", "퇴직금(퇴직연금) IRP 이전 신청", "퇴직일부터 14일 이내"),
        ("e4", "퇴직자 서류 발급", "원천징수영수증, 경력증명서 등 요청 시 발급"),
        ("e5", "다른 시스템 정리", "회계 프로그램 퇴사 반영, 기술인 경력 신고 변경, 이중가입 종료 등 해당하는 것"),
        ("e6", "PC 자료 최종 확인 후 초기화", "업무자료 보존을 확인한 뒤 시스템관리자만 초기화"),
    ]),
]
KEYS = [k for _s, _n, _w, items in STAGES for k, _t, _h in items]

DEFAULT_SUBJECT = "[{회사명}] 퇴직 절차 안내 ({이름}님)"
DEFAULT_BODY = """{이름}님, 안녕하세요. 경영관리팀입니다.

제출하신 사직서가 대표이사 결재로 최종 승인되어 퇴직일이 {퇴직일}로 확정되었습니다.
그동안의 노고에 감사드리며, 퇴직일까지 챙기셔야 할 사항을 순서대로 안내드립니다.

1. 업무 인수인계 (기한: {인수인계기한})
 - 인수인계서를 작성해 인수자({인수자})에게 전달하고 부서장 확인을 받아 주세요.
 - 인수인계서에는 주요 업무, 진행 현황, 계약·고객 대응 사항, 미완료 과제, 마감일, 관련 파일 경로를 적습니다.
 - 본인만 알고 있는 협의·약속 사항이 남지 않도록 빠짐없이 적어 주세요.
 - 인수인계는 퇴직일까지 정상 출근하여 마쳐 주셔야 합니다.

2. 업무자료 정리
 - 업무자료는 모두 회사 지정 저장소에 올려 주세요. 최종본뿐 아니라 편집할 수 있는 원본, 주요 중간본, 근거자료, 미완성·보류 자료도 포함합니다.
 - 바탕화면, 다운로드 폴더, 메신저 대화방에만 남아 있는 자료가 없는지 확인해 주세요.
 - 업무용 PC·노트북·저장매체는 포맷하거나 초기화하지 말고 그대로 반납해 주세요. 퇴직 준비나 개인자료 정리도 포맷 사유가 되지 않으며, 초기화는 확인을 마친 뒤 시스템관리자가 합니다.
 - 업무자료와 업무 메일을 한꺼번에 지우거나 옮기거나 외부로 보내지 말아 주세요.
 - PC에 있는 개인자료와 개인계정(메신저, 브라우저 로그인 등)은 시스템관리자와 미리 상의해 분리·삭제·로그아웃합니다.
 - 개인 기기, 개인 메일, 외부 클라우드에 회사 자료가 있다면 위치와 범위를 알려 주세요. 안내에 따라 반환·삭제합니다.

3. 연차
 - 남은 연차는 인수인계에 지장이 없도록 부서장과 일정을 협의해 사용합니다. 인수인계를 마치기 전에 한꺼번에 쓰는 것은 회사가 시기를 조정할 수 있습니다.
 - 쓰지 못한 연차는 퇴직 시 연차수당으로 정산합니다. 미리 당겨 쓴 연차가 있으면 마지막 급여에서 정산됩니다.

4. 퇴직일까지 반납·정리할 것
 - 업무용 PC·노트북 등 장비, 출입카드·열쇠, 법인카드, 회사 문서, 저장매체, 그 밖에 회사에서 받은 물품
 - 업무용 계정(메일, 그룹웨어, 저장소 등)은 퇴직일에 회수·이관합니다. 비밀번호를 임의로 바꾸거나 계정을 지우지 말아 주세요.
 - 아직 정산하지 않은 경비와 법인카드 사용분은 증빙과 함께 퇴직일 전에 정산해 주세요.

5. 급여·퇴직금
 - 마지막 급여와 퇴직금 등은 퇴직일부터 14일 이내에 지급합니다.
 - 퇴직금(퇴직연금)은 본인 명의 IRP 계좌로 이전합니다. 퇴직일 전에 IRP 계좌를 만들어 통장 사본을 경영관리팀에 내 주세요. 계좌가 없으면 지급이 늦어질 수 있습니다.
 - 4대보험료와 소득세는 퇴직 시 정산하여 마지막 급여에 반영하고 내역을 알려 드립니다.
 - 회사 지원으로 받은 외부 교육·자격 과정의 의무재직기간이 남아 있으면 지원금 반환 대상일 수 있습니다. 해당하는 경우 따로 안내드립니다.

6. 퇴직 후에도 지켜 주실 것
 - 재직 중 알게 된 회사와 고객의 비공개 정보는 퇴직 후 3년간 외부에 알리거나 업무 외 목적으로 사용하지 않습니다. 개인정보와 영업비밀은 기간과 관계없이 보호됩니다.

7. 퇴직 후 서류
 - 4대보험 자격상실 신고는 회사가 처리합니다.
 - 경력증명서, 원천징수영수증 등 필요한 서류는 경영관리팀에 요청하시면 발급해 드립니다.

퇴직일 전에 연락처, 주소, 급여계좌가 바뀌면 알려 주세요.
궁금한 점은 {담당자}에게 편하게 문의해 주세요.

감사합니다.
{회사명} 경영관리팀"""

FIELDS = ("이름", "퇴직일", "인수인계기한", "인수자", "담당자", "회사명")


def templates_text() -> dict:
    s = get_settings()
    return {"subject": s.get("offboard_subject") or DEFAULT_SUBJECT, "body": s.get("offboard_body") or DEFAULT_BODY,
            "custom": bool(s.get("offboard_subject") or s.get("offboard_body"))}


def _clean(subject: str, body: str) -> tuple[str, str]:
    return (subject or "").strip()[:200], (body or "").replace("\r\n", "\n").strip()[:8000]


def save_template(subject: str, body: str, reset: bool = False):
    subject, body = _clean(subject, body)
    with batch():
        set_setting("offboard_subject", "" if reset or subject == DEFAULT_SUBJECT else subject)
        set_setting("offboard_body", "" if reset or body == DEFAULT_BODY else body)
        audit("hr", "퇴사 안내문 기본 글귀 " + ("처음 글귀로 되돌림" if reset else "수정"), "", "")


def _kdate(v) -> str:
    d = to_date(v)
    return f"{d.year}년 {d.month}월 {d.day}일" if d else ""


def fill(text: str, emp: dict, ob: dict | None, sender: str) -> str:
    ob = ob or {}
    vals = {"이름": emp.get("name") or "", "퇴직일": _kdate(ob.get("last_day")) or "(퇴직일 미정)",
            "인수인계기한": _kdate(ob.get("handover_due")) or "퇴직일 전까지", "인수자": ob.get("successor") or "부서장이 지정",
            "담당자": sender or "경영관리팀 인사담당자", "회사명": get_settings().get("company_name", "")}
    for k in FIELDS:
        text = text.replace("{" + k + "}", vals[k])
    return text


def get(emp_id: int) -> dict | None:
    return query_one("SELECT * FROM offboardings WHERE employee_id=?", (emp_id,))


def save(emp_id: int, last_day, handover_due, successor: str, note: str) -> str:
    ld, hd = to_date(last_day), to_date(handover_due)
    if not ld:
        return "퇴직 예정일을 입력하세요"
    if hd and hd > ld:
        return "인수인계 기한은 퇴직 예정일보다 늦을 수 없습니다"
    vals = (ld.isoformat(), hd.isoformat() if hd else None, (successor or "").strip()[:50] or None, (note or "").strip()[:500] or None)
    with batch():
        if get(emp_id):
            execute("UPDATE offboardings SET last_day=?, handover_due=?, successor=?, note=? WHERE employee_id=?", vals + (emp_id,))
        else:
            execute("INSERT INTO offboardings(last_day, handover_due, successor, note, employee_id, created_by) VALUES (?,?,?,?,?,?)",
                    vals + (emp_id, current_actor.get()[1]))
            audit("hr", "퇴사 절차 시작", f"emp#{emp_id}", vals[0])
    return ""


def set_check(emp_id: int, key: str, done: bool) -> bool:
    if key not in KEYS or not get(emp_id):
        return False
    if done:
        execute("INSERT INTO offboard_checks(employee_id, key, done_by) VALUES (?,?,?) ON CONFLICT(employee_id, key) DO NOTHING",
                (emp_id, key, current_actor.get()[1]))
    else:
        execute("DELETE FROM offboard_checks WHERE employee_id=? AND key=?", (emp_id, key))
    return True


def checks(emp_id: int) -> dict:
    return {r["key"]: r for r in query("SELECT * FROM offboard_checks WHERE employee_id=?", (emp_id,))}


def set_closed(emp_id: int, closed: bool):
    with batch():
        execute("UPDATE offboardings SET closed_at=? WHERE employee_id=?", (today().isoformat() if closed else None, emp_id))
        audit("hr", "퇴사 절차 완료" if closed else "퇴사 절차 다시 열기", f"emp#{emp_id}", "")


def remove(emp_id: int):
    with batch():
        execute("DELETE FROM offboard_checks WHERE employee_id=?", (emp_id,))
        execute("DELETE FROM offboard_mails WHERE employee_id=?", (emp_id,))
        execute("DELETE FROM offboardings WHERE employee_id=?", (emp_id,))
        audit("hr", "퇴사 절차 기록 삭제", f"emp#{emp_id}", "")


def mails(emp_id: int) -> list[dict]:
    return query("SELECT * FROM offboard_mails WHERE employee_id=? ORDER BY id DESC", (emp_id,))


def log_notice(emp_id: int, how: str, to_addr: str, subject: str, body: str):
    """how: 메일(인사기록카드) / 메일(직접입력) / 직접 전달"""
    subject, body = _clean(subject, body)
    with batch():
        execute("INSERT INTO offboard_mails(employee_id, how, to_addr, subject, body, sent_by) VALUES (?,?,?,?,?,?)",
                (emp_id, how, enc(to_addr) if to_addr else None, subject, enc(body), current_actor.get()[1]))
        set_check(emp_id, "b1", True)
        audit("hr", "퇴사 안내문 " + ("전달 기록" if how == "직접 전달" else "메일 발송"), f"emp#{emp_id}", how)  # 주소 원문은 남기지 않음


def unfilled(text: str) -> bool:
    return any("{" + k + "}" in text for k in FIELDS)


def send(emp_id: int, to: str, how: str, subject: str, body: str) -> tuple[bool, str]:
    subject, body = _clean(subject, body)
    if not subject or not body:
        return False, "제목과 내용을 입력하세요"
    if unfilled(subject + body):
        return False, "채워지지 않은 항목({이름} 등)이 남아 있습니다"
    if not mailer.configured():
        return False, "메일 서버가 설정되지 않았습니다 (회사정보·환경설정 → 메일)"
    try:
        mailer.send(to, subject, body)
    except mailer.MailError as ex:
        audit("hr", "퇴사 안내문 발송 실패", f"emp#{emp_id}", str(ex)[:120])
        return False, str(ex)
    log_notice(emp_id, how, to, subject, body)
    return True, "안내문을 보냈습니다"


def rows() -> list[dict]:
    out = query("SELECT o.*, e.name, e.emp_no, e.department, e.rank, e.resign_date FROM offboardings o "
                "JOIN employees e ON e.id=o.employee_id ORDER BY (o.closed_at IS NOT NULL), o.last_day")
    done = {}
    for r in query("SELECT employee_id, key FROM offboard_checks"):
        if r["key"] in KEYS:
            done[r["employee_id"]] = done.get(r["employee_id"], 0) + 1
    sent = {r["employee_id"]: r["at"] for r in query("SELECT employee_id, MAX(sent_at) AS at FROM offboard_mails GROUP BY employee_id")}
    t = today()
    for r in out:
        r["done"], r["total"] = done.get(r["employee_id"], 0), len(KEYS)
        r["sent_at"] = sent.get(r["employee_id"])
        ld = to_date(r["last_day"])
        r["dday"] = (ld - t).days if ld else None
    return out


def dates(ob: dict | None) -> dict:
    """퇴직 예정일에서 계산한 기한 (참고용)."""
    ld = to_date((ob or {}).get("last_day"))
    if not ld:
        return {}
    return {"e": f"급여·퇴직금 지급 기한 {(ld + timedelta(days=14)).isoformat()}"}
