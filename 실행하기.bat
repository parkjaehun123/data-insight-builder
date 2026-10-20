@echo off
chcp 65001 > nul
title AI Data Insight Builder
echo ========================================================
echo   AI Data Insight Builder 서버를 실행합니다...
echo   브라우저에서 잠시 후 자동으로 창이 열립니다.
echo   종료하려면 이 창을 닫으시거나 Ctrl + C 를 누르세요.
echo ========================================================
cd /d "%~dp0"

start http://127.0.0.1:5000
.\.venv\Scripts\python.exe app.py
pause
