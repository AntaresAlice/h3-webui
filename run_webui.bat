@echo off
chcp 65001 >nul 2>nul
title MiniMax-H3 WebUI
rem ============================================================
rem   MiniMax-H3 WebUI Launcher
rem   Usage: run_webui.bat [ComfyUI_Root_Dir]
rem   ComfyUI root can also be set via env var H3_COMFY_ROOT.
rem ============================================================

set "COMFY_ROOT=D:\ComfyUI"
if defined H3_COMFY_ROOT set "COMFY_ROOT=%H3_COMFY_ROOT%"
if not "%~1"=="" set "COMFY_ROOT=%~1"

if not exist "%COMFY_ROOT%\python_embeded\python.exe" (
  echo [ERROR] ComfyUI python not found: "%COMFY_ROOT%\python_embeded\python.exe"
  echo   Pass your ComfyUI root as the first argument, e.g.:
  echo     run_webui.bat D:\ComfyUI
  echo   or set env var H3_COMFY_ROOT first.
  pause
  exit /b 1
)

set "PY=%COMFY_ROOT%\python_embeded\python.exe"
set "WEBUI=%~dp0webui\server.py"

echo ============================================================
echo   MiniMax-H3 WebUI Launcher
echo   ComfyUI root : %COMFY_ROOT%
echo ============================================================
echo.

REM --- [0] If WebUI already running on 8080, just open browser ---
curl -s -o nul -m 2 http://127.0.0.1:8080/ >nul 2>nul
if not errorlevel 1 (
  echo   WebUI already running. Opening browser...
  start "" http://127.0.0.1:8080
  goto :end
)

REM --- [1] Check ComfyUI on 8188 ---
echo [1/4] Checking ComfyUI at 127.0.0.1:8188 ...
curl -s -o nul -m 3 http://127.0.0.1:8188/system_stats >nul 2>nul
if not errorlevel 1 (
  echo   ComfyUI is already running.
  goto :start_webui
)

echo   ComfyUI not running. Starting in background...
start "ComfyUI" /D "%COMFY_ROOT%" "%PY%" -s ComfyUI\main.py --windows-standalone-build

echo   Waiting for ComfyUI to be ready (max 80s)...
set /a tries=0

:wait_loop
set /a tries+=1
if %tries% gtr 40 (
  echo.
  echo   [WARNING] ComfyUI did not start within 80 seconds.
  echo   Please start ComfyUI manually, then run this launcher again.
  echo.
  pause
  goto :end
)

timeout /t 2 /nobreak >nul
curl -s -o nul -m 2 http://127.0.0.1:8188/system_stats >nul 2>nul
if errorlevel 1 (
  echo   ...waiting [%tries%/40]
  goto :wait_loop
)

echo   ComfyUI ready after %tries% checks.

:start_webui
echo.
echo [2/4] Starting WebUI server at 127.0.0.1:8080 ...

echo [3/4] Opening browser...
timeout /t 2 /nobreak >nul
start "" http://127.0.0.1:8080

echo [4/4] Done.
echo.
echo ============================================================
echo   WebUI:  http://127.0.0.1:8080
echo   Keep this window open. Close it to stop WebUI.
echo   ComfyUI runs in its own window.
echo ============================================================
echo.

REM --- Run WebUI in foreground (Ctrl+C or close window to stop) ---
"%PY%" "%WEBUI%"

:end
pause
