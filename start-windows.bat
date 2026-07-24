@echo off
REM 채널 모니터 실행 (윈도우)
cd /d "%~dp0"

REM 필요한 라이브러리가 없으면 자동 설치
python -c "import flask" 2>NUL
if errorlevel 1 (
  echo 필요한 라이브러리를 설치합니다...
  pip install -r requirements.txt
)

python chanmon_app.py
pause
