import uvicorn
import socket
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.database import init_db
from web.app import create_app  # noqa: F401 — registers routes on `app`


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"


if __name__ == "__main__":
    init_db()
    ip = get_local_ip()
    port = 8000
    docs_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "documents"))
    print()
    print("  🚀 정량적 제안서 시스템 시작")
    print(f"  📡 접속 주소: http://{ip}:{port}")
    print(f"  💻 로컬 접속: http://localhost:{port}")
    print(f"  📁 서류 폴더: {docs_path}")
    print("  Ctrl+C 로 종료")
    print()
    uvicorn.run("web.app:app", host="0.0.0.0", port=port, reload=False)
