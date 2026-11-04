"""경영관리시스템 모듈 레지스트리.

새 업무 모듈(총무·재무 등)은 MODULES 에 메뉴를 추가하고 main.py 에서 router 를 등록하면
좌측 내비게이션에 자동으로 노출됩니다. ready=False 인 모듈은 '준비중'으로 표시됩니다.
"""

MODULES = [
    {
        "key": "hr", "name": "인사관리", "icon": "bi-people-fill", "ready": True,
        "menus": [
            {"key": "hr.dashboard", "name": "인사 대시보드", "icon": "bi-speedometer2", "url": "/hr/dashboard"},
            {"key": "hr.employees", "name": "임직원 현황", "icon": "bi-person-vcard", "url": "/hr/employees"},
            {"key": "hr.contracts", "name": "근로계약서", "icon": "bi-file-earmark-text", "url": "/hr/contracts"},
            {"key": "hr.salaries", "name": "연봉현황", "icon": "bi-graph-up-arrow", "url": "/hr/salaries"},
            {"key": "hr.rewards", "name": "상벌현황", "icon": "bi-award", "url": "/hr/rewards"},
            {"key": "hr.specials", "name": "근로특이사항", "icon": "bi-calendar-heart", "url": "/hr/specials"},
            {"key": "hr.certificates", "name": "증명서 발급", "icon": "bi-patch-check", "url": "/hr/certificates"},
            {"key": "hr.templates", "name": "문서 템플릿", "icon": "bi-file-earmark-ruled", "url": "/hr/templates"},
        ],
    },
    {
        "key": "ga", "name": "총무관리", "icon": "bi-building", "ready": False,
        "menus": [
            {"name": "자산·비품 관리"}, {"name": "법인차량 관리"}, {"name": "문서 수발·관인 대장"},
            {"name": "계약·임대차 관리"}, {"name": "경조사 관리"},
        ],
    },
    {
        "key": "fin", "name": "재무·회계", "icon": "bi-cash-coin", "ready": False,
        "menus": [{"name": "급여 대장"}, {"name": "경비 정산"}, {"name": "예산 관리"}],
    },
    {
        "key": "biz", "name": "영업·입찰", "icon": "bi-briefcase", "ready": False,
        "menus": [{"name": "정량적 제안서 (기존 시스템 연동 예정)"}],
    },
]
