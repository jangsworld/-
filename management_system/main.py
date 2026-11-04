"""경영관리업무시스템 실행: python main.py  (기본 포트 8100)"""
import os
import socket
import sys

import uvicorn

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8100))
    if "--no-seed" in sys.argv:
        os.environ["MGMT_SEED"] = "0"
    print()
    print("  🏢 경영관리업무시스템 시작 (인사관리 모듈)")
    print(f"  📡 내부망 접속: http://{local_ip()}:{port}")
    print(f"  💻 로컬 접속:   http://localhost:{port}")
    print("  Ctrl+C 로 종료")
    print()
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=False)
