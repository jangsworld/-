@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set BMS_HOST=127.0.0.1
title BMS 경영관리업무시스템 (테스트)
echo.
echo  [BMS] 노트북 테스트 실행을 준비합니다...
if not exist .venv\Scripts\python.exe (
  echo  [BMS] 처음 실행: 가상환경을 만들고 필요한 프로그램을 설치합니다. 1~3분 걸립니다.
  py -3 -m venv .venv 2>nul || python -m venv .venv
)
if not exist .venv\Scripts\python.exe (
  echo.
  echo  [오류] Python 을 찾을 수 없습니다. https://www.python.org/downloads/ 에서 Python 3.12 를 설치하고
  echo         설치 첫 화면에서 "Add python.exe to PATH" 를 꼭 체크한 뒤 다시 실행하세요.
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
  echo  [오류] 프로그램 설치에 실패했습니다. 인터넷 연결을 확인하세요.
  pause
  exit /b 1
)
echo.
echo  [BMS] 브라우저에서 http://localhost:8100 으로 접속하세요. (이 창을 닫으면 시스템이 종료됩니다)
start "" http://localhost:8100
.venv\Scripts\python.exe main.py --demo
pause
