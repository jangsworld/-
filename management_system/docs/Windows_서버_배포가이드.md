# BMS – Windows 관리서버 배포 가이드 (초안)

> 서버 환경 정보를 받으면 실제 경로·포트에 맞춰 이 문서를 확정합니다. (`docs/서버정보_요청서.md` 참고)

## 1. 이름 구분 규칙 (같은 서버의 다른 엔진과 충돌 방지)

| 항목 | BMS 사용값 | 설정 위치 |
|------|-----------|-----------|
| 설치 폴더 | `D:\BMS\app` | – |
| 데이터 / 키 / 로그 | `D:\BMS\data`, `D:\BMS\secrets`, `D:\BMS\logs` | `config\bms.toml [paths]` |
| Python 가상환경 | `D:\BMS\venv` (다른 엔진과 공유 금지) | – |
| 포트 | `8100` (사용 중이면 변경) | `[server] port` |
| Windows 서비스명 | `BMS-Web` | NSSM 등록 시 |
| DB 파일 | `bms.db` | 자동 |
| 세션 쿠키 | `bms_session` | 고정 |
| 환경변수 | `BMS_` 접두어 | – |
| NAS 폴더 | `\\NAS\BMS\BMS_인사\...`, `\\NAS\BMS\BMS_백업` | `[paths] nas_root` |
| 로그 파일 | `bms.log` (일 단위 회전, 400일 보관) | 자동 |

## 2. 설치 순서

```powershell
# 1) 폴더 준비
mkdir D:\BMS\app, D:\BMS\data, D:\BMS\secrets, D:\BMS\logs
# 2) 소스 배치 (git clone 또는 ZIP 해제 후 management_system 내용을 D:\BMS\app 으로)
# 3) 전용 가상환경
py -3.12 -m venv D:\BMS\venv
D:\BMS\venv\Scripts\pip install -r D:\BMS\app\requirements.txt
# 4) 설정
copy D:\BMS\app\config\bms.example.toml D:\BMS\app\config\bms.toml   # 값 수정
# 5) 최초 실행 → 콘솔에 초기 관리자 비밀번호 표시 (secrets\initial_admin_password.txt 에도 저장)
D:\BMS\venv\Scripts\python D:\BMS\app\main.py
# 6) 보안 점검
D:\BMS\venv\Scripts\python D:\BMS\app\manage.py check
```

PDF 변환은 Windows 기본 **Microsoft Edge** 를 사용하므로 별도 브라우저 설치가 필요 없습니다.

**입사서류·메일 추가 확인 사항**
- `employee_root` 는 `\\서버\공유\...` 형식으로 지정합니다. Windows 서비스에는 Z: 같은 드라이브 문자가 보이지 않습니다.
- 서비스 실행 계정에 해당 NAS 폴더 읽기/쓰기 권한이 있어야 합니다.
- 스캔본 판독에는 서버의 **한국어 OCR** 이 필요합니다. PowerShell 관리자 권한으로 설치합니다.
  `Add-WindowsCapability -Online -Name "Language.OCR~~~ko-KR~0.0.1.0"` (없으면 텍스트 PDF·HWP 만 읽음)
- 메일은 `[mail]` 을 설정하고 비밀번호를 `secrets\smtp_password.txt` 에 둡니다.
- 직원이 사내망 밖에서 서명하려면 `public_url` 에 HTTPS 주소를 넣고 `allowed_hosts` 에 도메인을 추가합니다. `/sign/` 경로만 로그인 없이 열립니다.

## 3. Windows 서비스 등록 (NSSM 예시)

```powershell
nssm install BMS-Web D:\BMS\venv\Scripts\python.exe D:\BMS\app\main.py
nssm set BMS-Web AppDirectory D:\BMS\app
nssm set BMS-Web ObjectName .\svc_bms <비밀번호>     # NAS 접근 권한을 가진 전용 계정
nssm set BMS-Web AppStdout D:\BMS\logs\service.log
nssm set BMS-Web Start SERVICE_AUTO_START
nssm start BMS-Web
```

- 서비스 계정 `svc_bms` 는 일반 사용자 권한 + `D:\BMS` 폴더 권한 + NAS `BMS` 공유 권한만 부여
- 서비스로 실행하면 매핑 드라이브(Z:)는 보이지 않으므로 NAS는 **UNC 경로**(`\\192.168.0.10\BMS`)로 설정

## 4. 폴더 권한 (관리자 PowerShell)

```powershell
icacls D:\BMS\secrets /inheritance:r /grant:r "svc_bms:(OI)(CI)R" "Administrators:(OI)(CI)F"
icacls D:\BMS\data    /inheritance:r /grant:r "svc_bms:(OI)(CI)M" "Administrators:(OI)(CI)F"
```

## 5. 백업 (작업 스케줄러, 매일 23:30)

```powershell
schtasks /create /tn "BMS-Backup" /sc daily /st 23:30 /ru svc_bms /rp <비밀번호> ^
  /tr "D:\BMS\venv\Scripts\python.exe D:\BMS\app\manage.py backup"
```

백업 DB는 암호화된 상태로 NAS `BMS_백업` 에 저장됩니다. 마스터키는 백업과 **다른 매체**에 보관하세요.

## 6. HTTPS (외부 접속 전 필수)

방식은 서버 정보 확인 후 확정합니다(VPN 권장). 리버스 프록시를 쓰는 경우 `bms.toml` 에서
`host = "127.0.0.1"`, `behind_proxy = true`, `allowed_hosts = ["실제 도메인"]` 으로 설정하고,
방화벽에서 8100 포트는 외부에 열지 않습니다.
