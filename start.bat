@echo off
chcp 65001 >nul
cd /d %~dp0

rem 已在运行（容器或本地）就直接开面板
netstat -ano | findstr ":8787" | findstr LISTENING >nul 2>&1
if not errorlevel 1 (
  echo Panel already running: http://127.0.0.1:8787
  start "" http://127.0.0.1:8787
  exit /b
)

rem 优先用 Docker 起容器
docker info >nul 2>&1
if not errorlevel 1 (
  echo Starting container...
  docker compose up -d
  timeout /t 6 /nobreak >nul
  start "" http://127.0.0.1:8787
  exit /b
)

rem Docker 不可用时退回本地 venv
echo Docker unavailable, running local python...
start "" http://127.0.0.1:8787
.venv\Scripts\python.exe app.py
