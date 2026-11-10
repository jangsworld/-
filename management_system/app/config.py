"""BMS(경영관리업무시스템) 설정 로더.

같은 서버의 다른 엔진과 구분되도록 모든 이름에 시스템 코드 'BMS' 를 사용합니다.
  - 환경변수 접두어 : BMS_
  - 설정 파일       : config/bms.toml  (BMS_CONFIG 로 경로 변경 가능)
  - 세션 쿠키       : bms_session
  - 기본 포트       : 8100
설정 파일이 없으면 아래 기본값으로 동작합니다(개발용). 운영은 config/bms.example.toml 을 복사해 작성.
"""
import os
import tomllib
from dataclasses import dataclass, field

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYSTEM_CODE = "BMS"
SYSTEM_NAME = "경영관리업무시스템"


@dataclass
class Settings:
    # [server]
    host: str = "0.0.0.0"
    port: int = 8100
    allowed_hosts: list = field(default_factory=lambda: ["*"])
    ssl_certfile: str = ""
    ssl_keyfile: str = ""
    behind_proxy: bool = False          # 리버스 프록시(HTTPS 종단) 뒤에서 구동 시 true
    trusted_proxies: list = field(default_factory=lambda: ["127.0.0.1"])
    # [paths]
    data_dir: str = os.path.join(APP_DIR, "data")
    secrets_dir: str = os.path.join(APP_DIR, "secrets")
    nas_root: str = ""                  # 예: \\\\192.168.0.10\\BMS  (비우면 data/outbox 사용)
    backup_dir: str = ""
    log_dir: str = os.path.join(APP_DIR, "logs")
    employee_root: str = ""             # 임직원 서류 폴더 예: \\\\NAS\\공유폴더\\인사\\BMS
    intake_interval_sec: int = 60       # 새 폴더 확인 주기(초), 0 이면 자동 감지 끔
    contract_root: str = ""             # 계약서 폴더 (계약별 하위 폴더 → 계약 등록 초안). 비우면 끔
    # [security]
    session_idle_minutes: int = 30
    session_max_hours: int = 10
    require_2fa: bool = True
    max_failed_logins: int = 5
    lockout_minutes: int = 15
    password_min_length: int = 10
    password_max_age_days: int = 90
    cookie_secure: str = "auto"         # auto / true / false
    max_upload_mb: int = 5
    # [pdf]
    pdf_browser_channel: str = "msedge" if os.name == "nt" else ""   # Windows 기본 Edge 사용
    pdf_browser_path: str = ""
    pdf_on_signed: bool = True          # 체결완료 저장 시 NAS 자동 보관
    # [mail]
    smtp_host: str = ""                 # 비우면 메일 대신 서명 링크를 화면에 표시
    smtp_port: int = 587
    smtp_ssl: bool = False              # true: SMTPS(465), false: STARTTLS(587)
    smtp_user: str = ""
    smtp_from: str = ""
    public_url: str = ""                # 직원이 메일 링크로 접속할 주소 예: https://bms.example.co.kr
    sign_valid_days: int = 7            # 서명 링크 유효기간(일)
    # [app]
    seed_sample: bool = False

    @property
    def https(self) -> bool:
        return bool(self.ssl_certfile) or self.behind_proxy

    @property
    def secure_cookie(self) -> bool:
        if self.cookie_secure == "auto":
            return self.https
        return str(self.cookie_secure).lower() == "true"

    @property
    def db_path(self) -> str:
        return os.environ.get("BMS_DB_PATH") or os.path.join(self.data_dir, "bms.db")

    @property
    def output_root(self) -> str:
        return self.nas_root or os.path.join(self.data_dir, "outbox")


def _load() -> Settings:
    s = Settings()
    path = os.environ.get("BMS_CONFIG") or os.path.join(APP_DIR, "config", "bms.toml")
    if os.path.exists(path):
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        for section in raw.values():
            if isinstance(section, dict):
                for k, v in section.items():
                    if hasattr(s, k):
                        setattr(s, k, v)
    # 환경변수 우선 (BMS_PORT, BMS_REQUIRE_2FA 등)
    for k in vars(s):
        env = os.environ.get(f"BMS_{k.upper()}")
        if env is None:
            continue
        cur = getattr(s, k)
        if isinstance(cur, bool):
            setattr(s, k, env.lower() in ("1", "true", "yes"))
        elif isinstance(cur, int):
            setattr(s, k, int(env))
        elif isinstance(cur, list):
            setattr(s, k, [x.strip() for x in env.split(",") if x.strip()])
        else:
            setattr(s, k, env)
    return s


settings = _load()
