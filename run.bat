@echo off
chcp 65001 > nul
title AI Data Insight Builder
echo ========================================================
echo   AI Data Insight Builder starting...
echo ========================================================
cd /d "%~dp0"

start http://127.0.0.1:5000
.\.venv\Scripts\python.exe app.py
pause
