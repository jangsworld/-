@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "VENV=%LOCALAPPDATA%\BMS-ecount-helper\venv"

if exist "%VENV%\Scripts\python.exe" goto run

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY (
  echo.
  echo  이 PC에 Python 이 없습니다.
  echo  https://www.python.org/downloads/ 에서 Python 3 를 설치한 뒤 다시 실행하세요.
  echo  ^(설치 첫 화면에서 "Add python.exe to PATH" 를 체크하세요^)
  pause
  exit /b 1
)
echo  처음 실행: 도우미 구성요소를 설치합니다 ^(1~2분^)...
%PY% -m venv "%VENV%" || goto fail
"%VENV%\Scripts\python.exe" -m pip install --disable-pip-version-check -q playwright || goto fail

:run
"%VENV%\Scripts\python.exe" -X utf8 "%~dp0ecount_helper.py"
exit /b 0

:fail
echo  설치에 실패했습니다. 인터넷 연결을 확인하고 다시 실행하세요.
pause
exit /b 1
