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
            {"key": "hr.contracts", "restricted": True, "name": "근로계약서", "icon": "bi-file-earmark-text", "url": "/hr/contracts"},
            {"key": "hr.salaries", "restricted": True, "name": "연봉현황", "icon": "bi-graph-up-arrow", "url": "/hr/salaries"},
            {"key": "hr.rewards", "restricted": True, "name": "상벌현황", "icon": "bi-award", "url": "/hr/rewards"},
            {"key": "hr.technicians", "restricted": True, "name": "기술자 현황", "icon": "bi-tools", "url": "/hr/technicians"},
            {"key": "hr.specials", "restricted": True, "name": "근로특이사항", "icon": "bi-calendar-heart", "url": "/hr/specials"},
            {"key": "hr.leave", "restricted": True, "name": "연차 관리", "icon": "bi-calendar-check", "url": "/hr/leave"},
            {"key": "hr.certificates", "restricted": True, "name": "증명서 발급", "icon": "bi-patch-check", "url": "/hr/certificates"},
            {"key": "hr.templates", "restricted": True, "name": "문서 템플릿", "icon": "bi-file-earmark-ruled", "url": "/hr/templates"},
        ],
    },
    {
        "key": "ga", "name": "총무관리", "icon": "bi-building", "ready": True,
        "menus": [
            {"key": "ga.dashboard", "name": "총무 대시보드", "icon": "bi-grid-1x2", "url": "/ga/dashboard"},
            {"key": "ga.assets", "name": "자산·비품", "icon": "bi-pc-display", "url": "/ga/assets"},
            {"key": "ga.vehicles", "name": "법인차량", "icon": "bi-car-front", "url": "/ga/vehicles"},
            {"key": "ga.docs", "name": "문서수발·관인", "icon": "bi-envelope-paper", "url": "/ga/docs"},
            {"key": "ga.contracts", "name": "계약·임대차", "icon": "bi-file-earmark-check", "url": "/ga/contracts"},
            {"key": "ga.events", "name": "경조사", "icon": "bi-flower1", "url": "/ga/events", "perm": "ga_events"},
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
