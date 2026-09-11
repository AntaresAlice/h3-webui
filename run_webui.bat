@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>nul
title MiniMax-H3 WebUI
rem ============================================================
rem   MiniMax-H3 WebUI Launcher  (Windows)
rem
rem   Usage:
rem     run_webui.bat                     default ComfyUI root
rem     run_webui.bat D:\ComfyUI          choose the ComfyUI root
rem     set H3WEBUI_PORT=9000 & run_webui.bat
rem     set H3_NO_BROWSER=1  & run_webui.bat   do not open a browser
rem
rem   Env vars: H3_COMFY_ROOT, H3WEBUI_PORT, H3_NO_BROWSER
rem   The launcher prints a log line for every step, so it never
rem   looks frozen: port picking, ComfyUI check, start command.
rem ============================================================

set "COMFY_ROOT=%~1"
if "%COMFY_ROOT%"=="" set "COMFY_ROOT=%H3_COMFY_ROOT%"
if "%COMFY_ROOT%"=="" set "COMFY_ROOT=D:\ComfyUI"
set "PY=%COMFY_ROOT%\python_embeded\python.exe"
set "WEBUI=%~dp0webui\server.py"
set "BASE_PORT=8080"
if defined H3WEBUI_PORT set "BASE_PORT=%H3WEBUI_PORT%"

echo ============================================================
echo   MiniMax-H3 WebUI Launcher
echo   ComfyUI root  : %COMFY_ROOT%
echo   ComfyUI python: %PY%
echo   WebUI script  : %WEBUI%
echo   WebUI port    : %BASE_PORT%  (shifts to the next free port if needed)
echo ============================================================
echo.

if not exist "%PY%" (
  echo [ERROR] ComfyUI python not found: "%PY%"
  echo   Pass your ComfyUI root as the first argument, e.g.:
  echo     run_webui.bat D:\ComfyUI
  echo   or set the H3_COMFY_ROOT environment variable first.
  goto :fail
)
if not exist "%WEBUI%" (
  echo [ERROR] WebUI server script not found: "%WEBUI%"
  echo   Run this launcher from inside the repository folder.
  goto :fail
)

rem --- [0] Resolve a usable WebUI port -------------------------
echo [0/4] Resolving a usable WebUI port - base port %BASE_PORT% ...
set "PORT_STATE="
set "PORT="
set "PORT_LOG=%TEMP%\h3_pick_port.log"
rem   端口探测日志 (含耗时) 会原样打印出来, 便于排查
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\pick_port.ps1" -BindHost 127.0.0.1 -Start %BASE_PORT% -Range 40 > "%PORT_LOG%" 2>&1
if exist "%PORT_LOG%" type "%PORT_LOG%"
for /f "usebackq tokens=1,2" %%a in ("%PORT_LOG%") do (
  if /i "%%a"=="RUNNING" ( set "PORT_STATE=RUNNING" & set "PORT=%%b" )
  if /i "%%a"=="FREE"    ( set "PORT_STATE=FREE" & set "PORT=%%b" )
  if /i "%%a"=="NONE"    ( set "PORT_STATE=NONE" & set "PORT=" )
)
if not defined PORT (
  echo.
  echo [ERROR] No usable WebUI port found in %BASE_PORT% .. %BASE_PORT%+39.
  echo   Pick another base port, e.g.:  set H3WEBUI_PORT=9000
  goto :fail
)
echo   Selected WebUI port: %PORT%   state: %PORT_STATE%
echo.

rem --- already running? then just open it ---
if /i "%PORT_STATE%"=="RUNNING" (
  echo   A WebUI is already answering on port %PORT% - nothing to start.
  call :open_ui
  goto :end
)

rem --- [1] ComfyUI -------------------------------------------------
echo [1/4] Checking ComfyUI at http://127.0.0.1:8188 ...
curl -s -o nul -m 3 http://127.0.0.1:8188/system_stats >nul 2>nul
if not errorlevel 1 (
  echo   ComfyUI is already running.
  echo.
  goto :start_webui
)

echo   ComfyUI is NOT running - starting it in a separate window:
echo     cd /d "%COMFY_ROOT%"
echo     "%PY%" -s ComfyUI\main.py --windows-standalone-build
start "ComfyUI" /D "%COMFY_ROOT%" "%PY%" -s ComfyUI\main.py --windows-standalone-build

echo   Waiting for ComfyUI to be ready - up to 80s ...
set /a tries=0
:wait_loop
set /a tries+=1
if %tries% gtr 40 (
  echo.
  echo   [WARNING] ComfyUI did not answer within 80 seconds.
  echo   Start ComfyUI manually, then run this launcher again.
  echo.
  goto :end
)
ping -n 3 127.0.0.1 >nul 2>nul
curl -s -o nul -m 2 http://127.0.0.1:8188/system_stats >nul 2>nul
if errorlevel 1 (
  echo   ...waiting for ComfyUI [%tries%/40]
  goto :wait_loop
)
echo   ComfyUI is ready after %tries% checks.
echo.

:start_webui
echo [2/4] Starting the WebUI server ...
echo   command: "%PY%" "%WEBUI%"
echo   cwd    : %~dp0
echo   port   : %PORT%  (exported as H3WEBUI_PORT)
echo.
call :open_ui
echo.
echo [3/4] WebUI URL: http://127.0.0.1:%PORT%
echo [4/4] Server logs follow. Keep this window open - Ctrl+C or closing it stops the WebUI.
echo ============================================================
echo.
set "H3WEBUI_PORT=%PORT%"
"%PY%" "%WEBUI%"
echo.
echo ============================================================
echo The WebUI server has exited.
goto :end

:open_ui
if defined H3_NO_BROWSER (
  echo   H3_NO_BROWSER is set - open this URL manually: http://127.0.0.1:%PORT%
  goto :eof
)
echo   Opening browser: http://127.0.0.1:%PORT%
start "" http://127.0.0.1:%PORT%
goto :eof

:fail
echo.
echo Launcher aborted - nothing was started.
:end
echo.
pause
