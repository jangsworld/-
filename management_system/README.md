# 경영관리업무시스템 (인사관리 모듈)

내부망용 경영관리 웹 시스템입니다. 첫 모듈로 **인사관리**를 제공하며, 총무·재무 모듈을 같은 구조로 추가할 수 있습니다.
기능 설계와 관리적 의미는 [docs/인사관리_기능설계.md](docs/인사관리_기능설계.md)를 참고하세요.

## 실행

```bash
cd management_system
pip install -r requirements.txt
python main.py              # http://localhost:8100  (최초 실행 시 샘플 데이터 생성)
python main.py --no-seed    # 샘플 데이터 없이 시작
```

- DB: `data/management.db` (SQLite, 백업은 파일 복사로 가능)
- 샘플 데이터를 지우고 새로 시작하려면 서버 종료 후 `data/management.db` 를 삭제하고 `--no-seed` 로 실행
- 테스트: `python -m pytest -q tests`

## 메뉴

| 메뉴 | 내용 |
|------|------|
| 인사 대시보드 | KPI, 인력 분포 차트, 법적 리스크·기한 알림 |
| 임직원 현황 | 기본 DB, 인사기록카드, 발령이력 자동기록, CSV 일괄등록/내보내기 |
| 근로계약서 | 구분별 자동작성, A4 에디터, 체결 상태·만료 관리, 미체결자 일괄작성 |
| 연봉현황 | 인상액·인상률, 그래프, 일괄 인상 시뮬레이션, 최저임금 점검 |
| 상벌현황 | 포상·징계, 말소일 자동계산, 통계 |
| 근로특이사항 | 육아휴직·단축근무·모성보호 등, 타임라인, 복직 알림 |
| 증명서 발급 | 재직·경력증명서, 발급번호 대장 |
| 문서 템플릿 | 계약서·증명서 양식 편집(치환자) |
| 설정 | 회사정보, 최저임금, 부서·직급 코드, 감사로그 |

## 구조

```
app/
  main.py            FastAPI 앱, 설정 화면
  db.py              SQLite 스키마·공통 쿼리
  common.py          템플릿 필터, 날짜·금액(한글) 유틸, HTML 정리
  seed.py            기본 템플릿·샘플 데이터
  modules/__init__.py  모듈·메뉴 레지스트리 (총무·재무 확장 지점)
  modules/hr/        인사 모듈 (constants, repo, services, routes, default_templates)
templates/           Jinja2 화면 (Bootstrap 5 + Chart.js)
static/              CSS / JS
```
