"""기본 템플릿 및 샘플 데이터 생성."""
import json
import random
from datetime import timedelta

from app.common import today, add_years, add_months, to_date
from app.db import query_one, execute, get_settings
from app.modules.hr import repo, services
from app.modules.hr.default_templates import DEFAULT_TEMPLATES


def ensure_templates():
    if query_one("SELECT COUNT(*) c FROM doc_templates")["c"]:
        return
    for group, cat, name, content in DEFAULT_TEMPLATES:
        repo.save_template(None, group, cat, name, content.strip())


# (성명, 성별, 출생연도, 부서, 직급, 직책, 고용형태, 입사 경과(개월 전), 기본 연봉(만원))
_SAMPLE = [
    ("김정훈", "남", 1972, "경영지원팀", "이사", "본부장", "정규직", 220, 9800),
    ("이수진", "여", 1983, "인사팀", "차장", "팀장", "정규직", 150, 7200),
    ("박민수", "남", 1986, "재무팀", "과장", "팀장", "정규직", 110, 6400),
    ("최영희", "여", 1990, "인사팀", "대리", "팀원", "정규직", 70, 4800),
    ("정우성", "남", 1979, "영업팀", "부장", "팀장", "정규직", 180, 8600),
    ("한지민", "여", 1992, "영업팀", "대리", "팀원", "정규직", 52, 4600),
    ("오세훈", "남", 1995, "영업팀", "주임", "팀원", "정규직", 30, 4000),
    ("윤아름", "여", 1991, "기술팀", "과장", "파트장", "정규직", 84, 5900),
    ("강동원", "남", 1984, "기술팀", "차장", "팀장", "정규직", 132, 7400),
    ("서지혜", "여", 1996, "기술팀", "사원", "팀원", "정규직", 18, 3600),
    ("임재현", "남", 1988, "연구소", "부장", "연구소장", "정규직", 96, 8200),
    ("송하늘", "여", 1993, "연구소", "대리", "팀원", "정규직", 44, 5000),
    ("배준호", "남", 1998, "연구소", "사원", "팀원", "계약직(기간제)", 21, 3500),
    ("조은비", "여", 1999, "경영지원팀", "사원", "팀원", "계약직(기간제)", 8, 3200),
    ("황보람", "여", 1975, "경영지원팀", "사원", "팀원", "단시간(파트타임)", 26, 1600),
    ("문성호", "남", 1960, "기술팀", "부장", "팀원", "촉탁직", 14, 5200),
    ("신예린", "여", 2001, "영업팀", "사원", "팀원", "인턴", 3, 3000),
    ("노태양", "남", 1994, "재무팀", "주임", "팀원", "정규직", 1, 4100),
    ("구민재", "남", 1989, "영업팀", "과장", "팀원", "정규직", 60, 5600),   # 퇴사자
    ("유채원", "여", 1997, "기술팀", "사원", "팀원", "계약직(기간제)", 30, 3400),  # 퇴사자
]


def seed_sample():
    if query_one("SELECT COUNT(*) c FROM employees")["c"]:
        return
    rnd = random.Random(2026)
    t = today()
    emp_ids = {}
    for i, (name, gender, by, dept, rank, title, etype, months_ago, sal) in enumerate(_SAMPLE):
        hire = add_months(t, -months_ago).replace(day=1 + (i * 3) % 27)
        data = {
            "name": name, "gender": gender,
            "birth_date": f"{by}-{rnd.randint(1, 12):02d}-{rnd.randint(1, 28):02d}",
            "phone": f"010-{rnd.randint(1000, 9999)}-{rnd.randint(1000, 9999)}",
            "email": f"user{i + 1:02d}@sample.co.kr",
            "address": rnd.choice(["서울특별시 강남구 테헤란로 1", "경기도 성남시 분당구 판교로 2",
                                   "서울특별시 마포구 월드컵북로 3", "인천광역시 연수구 송도과학로 4"]),
            "department": dept, "rank": rank, "title": title, "employment_type": etype,
            "job_duty": {"경영지원팀": "총무·경영지원", "인사팀": "인사·노무", "재무팀": "회계·자금",
                         "영업팀": "영업·고객관리", "기술팀": "시스템 구축·유지보수", "연구소": "연구개발"}[dept],
            "work_location": "본사", "hire_date": hire.isoformat(),
            "probation_end": add_months(hire, 3).isoformat() if etype == "정규직" else None,
            "bank_name": rnd.choice(["국민은행", "신한은행", "우리은행", "하나은행"]),
            "bank_account": f"{rnd.randint(100, 999)}-{rnd.randint(10, 99)}-{rnd.randint(100000, 999999)}",
            "education": rnd.choice(["학사", "학사", "석사", "전문학사"]),
            "emergency_contact": f"배우자 010-{rnd.randint(1000, 9999)}-{rnd.randint(1000, 9999)}",
        }
        emp_id = repo.create_employee(data)
        emp_ids[name] = emp_id

        # 연봉 이력: 입사연도부터 올해까지
        cur = sal * 10000
        hist = []
        for y in range(t.year, hire.year - 1, -1):
            hist.append((y, cur))
            rate = rnd.choice([0.025, 0.03, 0.035, 0.04, 0.045, 0.05, 0.06, 0.08])
            cur = round(cur / (1 + rate) / 100000) * 100000
        for y, amt in reversed(hist[:4]):
            reason = "신규" if y == hire.year else ("승진" if amt > 0 and rnd.random() < 0.15 else "정기인상")
            repo.upsert_salary(emp_id, y, amt, hire.isoformat() if y == hire.year else f"{y}-01-01", reason)

    # 퇴사자 처리
    repo.update_employee(emp_ids["구민재"], {"resign_date": add_months(t, -2).isoformat(), "resign_reason": "개인사유(이직)"})
    repo.update_employee(emp_ids["유채원"], {"resign_date": add_months(t, -5).isoformat(), "resign_reason": "계약기간 만료"})

    # 근로계약서: 대부분 체결완료, 일부 미체결(대시보드 알림 확인용)
    tpls = {tp["category"]: tp for tp in repo.list_templates("contract")}
    no_contract = {"노태양", "신예린"}
    for name, emp_id in emp_ids.items():
        if name in no_contract:
            continue
        emp = repo.get_employee(emp_id)
        cat = services.suggest_template_category(emp)
        params = services.default_contract_params(emp, cat)
        if params["end_date"]:  # 기간제·단시간: 매년 재계약된 것으로 가정 → 최근 계약 회차 기준
            hd = to_date(emp["hire_date"])
            yrs = max(0, (t - hd).days // 365)
            st = add_years(hd, yrs)
            params["start_date"] = st.isoformat()
            params["end_date"] = (add_years(st, 1) - timedelta(days=1)).isoformat()
        if cat == "기간제" and name == "조은비":
            params["end_date"] = (t + timedelta(days=40)).isoformat()   # 만료 임박
        content = services.render_template(tpls[cat]["content"], services.build_context(emp, params))
        repo.create_document(
            doc_group="contract", employee_id=emp_id, template_id=tpls[cat]["id"], category=cat,
            title=f"{tpls[cat]['name']} - {name}", start_date=params["start_date"],
            end_date=params["end_date"] or None, annual_salary=int(params["annual_salary"] or 0) or None,
            weekly_hours=params["weekly_hours"], params=json.dumps(params, ensure_ascii=False),
            content=content, status="체결완료", signed_date=params["start_date"],
        )
    # 올해 연봉계약서 (일부 서명대기)
    for name in ["이수진", "최영희", "윤아름"]:
        emp = repo.get_employee(emp_ids[name])
        params = services.default_contract_params(emp, "연봉계약")
        content = services.render_template(tpls["연봉계약"]["content"], services.build_context(emp, params))
        repo.create_document(
            doc_group="contract", employee_id=emp["id"], template_id=tpls["연봉계약"]["id"], category="연봉계약",
            title=f"연봉계약서 {t.year} - {name}", start_date=params["start_date"], end_date=params["end_date"],
            annual_salary=emp["annual_salary"], params=json.dumps(params, ensure_ascii=False), content=content,
            status="서명대기" if name == "윤아름" else "체결완료",
        )

    # 상벌
    rw = [
        ("이수진", "포상", "우수사원", -10, "인사제도 개편 기여", 1000000),
        ("강동원", "포상", "공로상", -6, "대형 프로젝트 성공적 수행", 2000000),
        ("한지민", "포상", "표창", -3, "분기 영업실적 1위", 500000),
        ("윤아름", "포상", "제안상", -14, "업무 자동화 제안", 300000),
        ("오세훈", "징계", "견책", -4, "근태 불량(지각 반복)", 0),
        ("구민재", "징계", "감봉", -9, "회사 규정 위반", 0),
        ("김정훈", "포상", "장기근속", -1, "근속 15주년", 1500000),
    ]
    for name, kind, cat, m, reason, amt in rw:
        ad = add_months(t, m)
        repo.save_reward(None, {
            "employee_id": emp_ids[name], "kind": kind, "category": cat, "action_date": ad.isoformat(),
            "reason": reason, "amount": amt, "issuer": "대표이사",
            "expunge_date": add_months(ad, 12 if cat == "견책" else 18).isoformat() if kind == "징계" else None,
            "period_start": ad.isoformat() if cat == "감봉" else None,
            "period_end": add_months(ad, 3).isoformat() if cat == "감봉" else None,
        })

    # 근로특이사항
    sp = [
        ("송하늘", "육아휴직", -8, 4, None, "첫째", -9),
        ("최영희", "육아기 근로시간 단축", -3, 9, 30, "첫째", -40),
        ("윤아름", "임신기 근로시간 단축", -1, 2, 30, None, None),
        ("강동원", "배우자 출산휴가", -2, 0, None, "둘째", -2),
        ("서지혜", "재택근무", -1, 5, None, None, None),
        ("이수진", "가족돌봄휴직", -14, -13, None, None, None),
    ]
    for name, cat, sm, em, hours, child, cb in sp:
        start = add_months(t, sm)
        end = add_months(t, em) if em else start + timedelta(days=19)
        repo.save_special(None, {
            "employee_id": emp_ids[name], "category": cat, "start_date": start.isoformat(),
            "end_date": (end - timedelta(days=1)).isoformat(), "reduced_hours": hours,
            "child_name": child, "child_birth": add_months(t, cb).isoformat() if cb else None,
            "replacement": "대체인력 채용(계약직)" if cat == "육아휴직" else None,
            "subsidy": "육아휴직 지원금 신청완료" if cat == "육아휴직" else
                       ("워라밸일자리 장려금 신청" if "단축" in cat else None),
        })


def seed_ga_sample():
    """총무 샘플: 자산·차량·운행일지·문서·관인·계약·경조사."""
    from app.modules.ga import repo as ga
    if query_one("SELECT COUNT(*) c FROM ga_assets")["c"]:
        return
    t = today()
    rnd = random.Random(7)
    emps = {e["name"]: e["id"] for e in query_all_emps()}

    def d(months=0, days=0):
        return (add_months(t, months) + timedelta(days=days)).isoformat()

    assets = [
        ("IT기기", "노트북", "LG gram 16", 1890000, -30, "김정훈"), ("IT기기", "노트북", "LG gram 16", 1890000, -30, "이수진"),
        ("IT기기", "노트북", "Dell Latitude 5440", 1650000, -14, "강동원"), ("IT기기", "노트북", "Dell Latitude 5440", 1650000, -14, "윤아름"),
        ("IT기기", "노트북", "삼성 갤럭시북4", 1450000, -8, "한지민"), ("IT기기", "모니터", "LG 27UP850", 520000, -20, "박민수"),
        ("IT기기", "데스크톱", "삼성 DM500", 1100000, -52, "최영희"), ("IT기기", "노트북", "LG gram 15", 1590000, -40, "구민재"),
        ("사무기기", "복합기", "신도리코 D420", 4200000, -36, None), ("사무기기", "빔프로젝터", "엡손 EB-FH52", 980000, -26, None),
        ("가구·집기", "회의 테이블(12인)", "퍼시스 CHT", 2300000, -60, None), ("가구·집기", "사무용 의자", "시디즈 T50", 320000, -24, "서지혜"),
        ("통신기기", "업무용 휴대폰", "갤럭시 S24", 1150000, -10, "정우성"), ("소프트웨어·라이선스", "MS 365 Business (20석)", "연간 구독", 3600000, -11, None),
        ("소프트웨어·라이선스", "한컴오피스 2024 (10석)", "영구 라이선스", 2100000, -22, None), ("IT기기", "NAS", "Synology DS923+", 1350000, -18, None),
    ]
    for i, (cat, name, model, cost, m, holder) in enumerate(assets):
        acq = d(m)
        warranty = d(m + (12 if cat == "소프트웨어·라이선스" else 36))
        if name.startswith("MS 365"):
            warranty = d(0, 20)  # 갱신 임박
        elif model == "영구 라이선스":
            warranty = None
        aid = ga.save("ga_assets", None, {
            "asset_no": ga.next_asset_no(cat, acq), "category": cat, "name": name, "model": model,
            "serial_no": f"SN{rnd.randint(100000, 999999)}", "vendor": rnd.choice(["다나와몰", "컴퓨존", "OO시스템"]),
            "acquired_date": acq, "cost": cost, "useful_years": {"IT기기": 4, "사무기기": 5, "가구·집기": 8, "통신기기": 4}.get(cat, 1),
            "location": "본사 3층 사무실" if holder else rnd.choice(["본사 4층 회의실", "본사 창고"]),
            "status": "사용중" if holder else ("사용중" if cat in ("사무기기", "소프트웨어·라이선스") else "보관"),
            "warranty_end": warranty,
        })
        ga.add_asset_event(aid, acq, "취득", None, cost, "신규 구매")
        if holder and holder in emps:
            ga.add_asset_event(aid, acq, "지급", emps[holder], 0, "업무용 지급")

    v1 = ga.save("ga_vehicles", None, {"plate_no": "123가 4567", "model": "현대 쏘나타 2.0", "model_year": t.year - 3, "fuel": "휘발유",
                                       "ownership": "소유", "acquired_date": d(-38), "insurer": "삼성화재", "insurance_end": d(0, 25),
                                       "inspection_due": d(4), "manager_id": emps.get("박민수"), "status": "운행", "odometer_start": 48200})
    v2 = ga.save("ga_vehicles", None, {"plate_no": "45허 7890", "model": "기아 카니발 하이리무진", "model_year": t.year - 1, "fuel": "경유",
                                       "ownership": "장기렌트", "acquired_date": d(-14), "lease_end": d(2), "monthly_cost": 1150000,
                                       "insurer": "렌트사 일괄", "insurance_end": d(22), "inspection_due": d(10),
                                       "manager_id": emps.get("정우성"), "status": "운행", "odometer_start": 12000})
    drivers = [emps[n] for n in ("정우성", "한지민", "오세훈", "강동원", "박민수") if n in emps]
    places = ["서울시청 계약 협의", "판교 고객사 미팅", "인천공항 출장 픽업", "수원 현장 점검", "세무서 서류 제출", "협력사 방문"]
    for vid, start in ((v1, 48200), (v2, 12000)):
        km = start
        day = add_months(t, -10)
        while day < t:
            dist = rnd.randint(18, 160)
            purpose = rnd.choices(["일반업무", "출퇴근", "비업무"], [80, 15, 5])[0]
            ga.save("ga_vehicle_logs", None, {"vehicle_id": vid, "drive_date": day.isoformat(), "driver_id": rnd.choice(drivers),
                                              "purpose": purpose, "destination": rnd.choice(places), "km_start": km, "km_end": km + dist,
                                              "fuel_cost": rnd.choice([0, 0, 65000, 80000]), "toll_cost": rnd.choice([0, 0, 3200, 7500])})
            km += dist
            day += timedelta(days=rnd.randint(3, 8))
        for m, cat, det, cost in ((-9, "소모품", "엔진오일·필터 교체", 98000), (-5, "정비", "브레이크 패드 교체", 210000),
                                  (-2, "자동차세", "상반기 자동차세", 260000)):
            ga.save("ga_vehicle_maint", None, {"vehicle_id": vid, "maint_date": d(m), "category": cat, "detail": det, "cost": cost,
                                               "vendor": "블루핸즈 강남점"})

    prefix = get_settings().get("doc_prefix", "총무")
    docs = [("수신", -2, "서울특별시청 정보통신과", "정보시스템 유지관리 용역 입찰 공고 안내", "회신필요", 5),
            ("수신", -1, "국민건강보험공단", "건강보험료 정산 결과 통지", "완료", None),
            ("발신", -1, "(주)OO건설", "공동수급 협정서 송부", "완료", None),
            ("발신", 0, "강남세무서", "법인세 경정청구 관련 자료 제출", "완료", None),
            ("수신", 0, "고용노동부 서울강남지청", "근로감독 자율점검표 제출 요청", "회신필요", 12),
            ("발신", 0, "(주)샘플파트너스", "유지보수 계약 갱신 의사 회신", "완료", None)]
    for direction, m, cp, title, status, due in docs:
        dd = d(m, -rnd.randint(0, 20))
        ga.save("ga_docs", None, {"direction": direction, "doc_no": ga.next_doc_no(direction, dd, prefix), "doc_date": dd,
                                  "counterpart": cp, "title": title, "department": "경영지원팀", "handler_id": emps.get("이수진"),
                                  "method": rnd.choice(["전자문서", "이메일", "등기"]), "status": status,
                                  "due_date": d(0, due) if due else None, "retention": "5년"})
    for m, typ, purpose, status in ((-3, "법인인감", "본사 임대차 재계약서 날인", "사용완료"), (-1, "사용인감", "입찰 참가 신청서", "사용완료"),
                                     (0, "법인인감", "은행 운전자금 대출 약정", "신청"), (0, "직인", "재직증명서 발급", "승인")):
        ga.save("ga_seal_logs", None, {"use_date": d(m, -rnd.randint(0, 5)), "seal_type": typ, "purpose": purpose,
                                       "submit_to": rnd.choice(["OO은행", "조달청", "임대인"]), "copies": 1,
                                       "requester_id": emps.get("박민수"), "status": status,
                                       "approver": "시스템관리자" if status != "신청" else None})
    contracts = [("임대차", "본사 사무실 임대차", "(주)OO빌딩관리", -22, 2, 8500000, "월", 100000000, 0, 90),
                 ("리스·렌탈", "복합기 렌탈", "신도리코", -30, 6, 180000, "월", 0, 1, 30),
                 ("유지보수", "그룹웨어 유지보수", "(주)샘플파트너스", -11, 1, 3300000, "연", 0, 1, 60),
                 ("용역", "건물 청소 용역", "OO클린", -8, 4, 1200000, "월", 0, 1, 30),
                 ("통신·IT서비스", "인터넷 전용회선", "KT", -18, 6, 330000, "월", 0, 1, 30),
                 ("보험", "영업배상책임보험", "현대해상", -10, 2, 2400000, "연", 0, 0, 30),
                 ("용역", "세무 기장 대리", "OO세무회계", -34, 2, 550000, "월", 0, 1, 60)]
    for cat, title, cp, sm, em, amt, cyc, dep, auto, notice in contracts:
        ga.save("ga_contracts", None, {"category": cat, "title": title, "counterparty": cp, "start_date": d(sm),
                                       "end_date": (add_months(t, em) - timedelta(days=1)).isoformat(), "amount": amt, "pay_cycle": cyc,
                                       "deposit": dep, "auto_renew": auto, "notice_days": notice, "manager_id": emps.get("이수진"),
                                       "status": "유효"})
    rules = {(r["category"], r["relation"]): r for r in ga.event_rules()}
    for name, cat, rel, m, status in (("한지민", "결혼", "본인", -7, "지급완료"), ("강동원", "출산", "배우자", -2, "지급완료"),
                                      ("임재현", "사망", "부모", -4, "지급완료"), ("송하늘", "결혼", "형제자매", -1, "지급완료"),
                                      ("오세훈", "사망", "조부모", 0, "신청")):
        r = rules[(cat, rel)]
        ed = d(m, -5)
        ga.save("ga_events", None, {"employee_id": emps[name], "category": cat, "relation": rel, "event_date": ed,
                                    "amount": r["amount"], "leave_days": r["leave_days"], "flower": r["flower"], "status": status,
                                    "paid_date": ed if status == "지급완료" else None})


def query_all_emps():
    from app.db import query
    return query("SELECT id, name FROM employees")
