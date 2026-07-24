#!/bin/bash
# 채널 모니터 실행 (맥)
cd "$(dirname "$0")" || exit 1

# 필요한 라이브러리가 없으면 자동 설치
python3 -c "import flask" 2>/dev/null || {
  echo "필요한 라이브러리를 설치합니다..."
  pip3 install -r requirements.txt
}

python3 chanmon_app.py
