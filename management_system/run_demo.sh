#!/usr/bin/env bash
# macOS / Linux 테스트 실행: ./run_demo.sh  → http://localhost:8100
set -e
cd "$(dirname "$0")"
export BMS_HOST=127.0.0.1
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt
echo "  [BMS] 브라우저에서 http://localhost:8100 으로 접속하세요 (Ctrl+C 로 종료)"
( sleep 3; command -v open >/dev/null && open http://localhost:8100 || true ) &
exec .venv/bin/python main.py --demo
