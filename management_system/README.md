# 경영관리업무시스템 (인사관리 모듈)

경영관리 웹 시스템입니다. **인사관리**·**총무관리** 모듈을 제공하며, 재무 등 다른 모듈을 같은 구조로 추가할 수 있습니다.
기능 설계와 관리적 의미: [인사관리](docs/인사관리_기능설계.md) · [총무관리](docs/총무관리_기능설계.md)

## 실행

> **노트북 테스트**: Windows 는 `run_demo.bat` 더블클릭 → http://localhost:8100 · 자세한 방법은 [docs/노트북_테스트_가이드.md](docs/노트북_테스트_가이드.md)

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
| 임직원 현황 | 기본 DB, 인사기록카드, 발령이력 자동기록, CSV 일괄등록/내보내기, **입사서류 폴더 자동 감지 → 등록 초안(확인필요)** |
| 근로계약서 | 구분별 자동작성, A4 에디터, **직원 메일 전자서명**, 체결 상태·만료 관리, 미체결자 일괄작성, 임직원 폴더 PDF 저장 |
| 연봉현황 | 인상액·인상률, 그래프, 일괄 인상 시뮬레이션, 최저임금 점검 |
| 상벌현황 | 포상·징계, 말소일 자동계산, 통계 |
| 근로특이사항 | 육아휴직·단축근무·모성보호 등, 타임라인, 복직 알림 |
| 증명서 발급 | 재직·경력증명서, 발급번호 대장, 임직원 폴더 PDF 자동 저장, 직원 메일 전송 |
| 문서 템플릿 | 계약서·증명서 양식 편집(치환자) |
| 총무 대시보드 | 자산·차량·계약·경조사 KPI, 만기·통보기한·미반납 알림 |
| 자산·비품 | 자산번호 자동부여, 감가상각 장부가, 지급·반납 이력, QR 라벨, 자산대장 |
| 법인차량 | 차량대장, 운행일지, 업무사용비율, 운행기록부 CSV, 정비·보험·검사 |
| 문서수발·관인 | 수신·발신 자동 채번, 회신기한, 관인 사용 신청·승인 |
| 계약·임대차 | 월 고정비 환산, 보증금, 자동갱신·해지 통보기한 |
| 경조사 | 경조 규정 자동 적용, 지급 관리 |
| 사용자·권한 관리 | 계정 생성(임시 비밀번호), 권한(관리자/인사·총무담당/총무담당/조회자), 잠금해제, 비밀번호·2FA 초기화 |
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
  modules/ga/        총무 모듈 (constants, repo, services, routes)
templates/           Jinja2 화면 (Bootstrap 5 + Chart.js)
static/              CSS / JS
```
