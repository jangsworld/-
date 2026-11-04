# 경영관리업무시스템 (인사관리 모듈)

내부망용 경영관리 웹 시스템입니다. 첫 모듈로 **인사관리**를 제공하며, 총무·재무 모듈을 같은 구조로 추가할 수 있습니다.
기능 설계와 관리적 의미는 [docs/인사관리_기능설계.md](docs/인사관리_기능설계.md)를 참고하세요.

## 실행

```bash
cd management_system
pip install -r requirements.txt
python main.py --demo        # 데모: 샘플 임직원 20명 + 초기 관리자 계정 생성
python main.py               # 운영: config/bms.toml 사용 (config/bms.example.toml 참고)
python manage.py check       # 보안 설정 점검
```

- 최초 실행 시 **초기 관리자(admin)** 와 임시 비밀번호가 콘솔과 `secrets/initial_admin_password.txt` 에 표시됩니다.
  첫 로그인에서 **2단계 인증(OTP) 등록 → 비밀번호 변경**이 강제되며, 이후 파일은 자동 삭제됩니다.
- 암호화 마스터키 `secrets/bms_master.key` 는 **반드시 별도 백업** (분실 시 데이터 복구 불가)
- 보안 설계: [docs/보안설계.md](docs/보안설계.md) · 서버 배포: [docs/Windows_서버_배포가이드.md](docs/Windows_서버_배포가이드.md)
  · 서버 정보 전달 방법: [docs/서버정보_요청서.md](docs/서버정보_요청서.md)
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
| 사용자·권한 관리 | 계정 생성(임시 비밀번호), 권한(관리자/인사담당/조회자), 잠금해제, 비밀번호·2FA 초기화 |
| 접속기록 | 로그인·열람·수정·내보내기 기록 조회 및 CSV (개인정보 접속기록) |
| 설정 | 회사정보, 최저임금, 부서·직급 코드, 서버 구동 설정 확인 |

## 구조

```
app/
  main.py            FastAPI 앱, 설정 화면
  db.py              SQLite 스키마·공통 쿼리
  common.py          템플릿 필터, 날짜·금액(한글) 유틸, HTML 정리
  seed.py            기본 템플릿·샘플 데이터
  config.py          BMS 설정 로더 (config/bms.toml, BMS_ 환경변수)
  storage.py         PDF 변환·NAS 보관·무결성 검증
  security/          crypto(AES-256-GCM) · auth(Argon2id·세션·TOTP) · middleware(권한·CSRF·헤더) · routes
  modules/__init__.py  모듈·메뉴 레지스트리 (총무·재무 확장 지점)
  modules/hr/        인사 모듈 (constants, repo, services, routes, default_templates)
templates/           Jinja2 화면 (Bootstrap 5 + Chart.js)
static/              CSS / JS
```
