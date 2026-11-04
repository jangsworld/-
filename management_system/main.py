"""BMS 경영관리업무시스템 실행.

  python main.py            운영 모드 (config/bms.toml 사용)
  python main.py --demo     샘플 데이터가 들어간 데모 DB로 실행(신규 DB일 때만)
"""
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if "--demo" in sys.argv:
    os.environ["BMS_SEED_SAMPLE"] = "true"

import uvicorn  # noqa: E402

from app.config import settings, SYSTEM_CODE  # noqa: E402


def local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"


if __name__ == "__main__":
    scheme = "https" if settings.ssl_certfile else "http"
    print()
    print(f"  🏢 [{SYSTEM_CODE}] 경영관리업무시스템 시작")
    print(f"  📡 접속 주소: {scheme}://{local_ip()}:{settings.port}")
    print(f"  🗄  DB: {settings.db_path}")
    print(f"  📁 문서 보관: {settings.output_root}")
    if not settings.https:
        print("  ⚠  HTTPS 미설정: 외부 접속 전 반드시 HTTPS(인증서 또는 리버스 프록시)를 구성하세요")
    print("  Ctrl+C 로 종료\n")
    opts = dict(host=settings.host, port=settings.port, reload=False, server_header=False,
                proxy_headers=settings.behind_proxy, forwarded_allow_ips=",".join(settings.trusted_proxies))
    if settings.ssl_certfile:
        opts.update(ssl_certfile=settings.ssl_certfile, ssl_keyfile=settings.ssl_keyfile)
    uvicorn.run("app.main:app", **opts)
