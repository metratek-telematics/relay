@echo off
rem relay-phone: share the Android phone plugged into this Windows PC with a Relay that runs on another machine.
rem Copy this file to the PC the phone is plugged into and keep its window open while agents use the phone.
rem
rem   relay-phone.bat                         network (Tailscale, VPN or LAN): adb listens on this PC's network addresses.
rem   relay-phone.bat net 192.168.1.0/24      same, allowing another address range through the firewall
rem                                           (default 100.64.0.0/10, Tailscale). Needs Administrator once per range.
rem   relay-phone.bat ssh user@server [bind]  SSH reverse tunnel to the Relay host. bind is where the tunnel listens there:
rem                                           172.17.0.1 (default, Relay in Docker) or 127.0.0.1 (Relay without Docker).
rem
rem Then in Relay: Settings > Verification > Android phone > Through my PC, and the address this window prints.
rem Setup and troubleshooting: docs/RUNNING.md, section "Android phone".
setlocal
title Relay phone

set "ADB="
for %%A in (adb.exe) do set "ADB=%%~$PATH:A"
if not defined ADB if exist "%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe" set "ADB=%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe"
if not defined ADB (
  echo adb.exe was not found. Install Android SDK Platform-Tools
  echo   https://developer.android.com/tools/releases/platform-tools
  echo and add its folder to PATH, or install Android Studio.
  pause
  exit /b 1
)

if /i "%~1"=="ssh" goto ssh

rem ------------------------------------------------------------------ network
set "RANGE=%~2"
if not defined RANGE set "RANGE=100.64.0.0/10"
set "RULE=Relay adb"
netsh advfirewall firewall show rule name="%RULE%" >nul 2>&1
if errorlevel 1 (
  netsh advfirewall firewall add rule name="%RULE%" dir=in action=allow program="%ADB%" protocol=TCP localport=5037 remoteip=%RANGE% profile=any >nul 2>&1
  if errorlevel 1 goto needadmin
) else if not "%~2"=="" (
  netsh advfirewall firewall set rule name="%RULE%" new program="%ADB%" remoteip=%RANGE% >nul 2>&1
  if errorlevel 1 goto needadmin
)

"%ADB%" kill-server >nul 2>&1
echo.
echo   Sharing the phone on port 5037, reachable from %RANGE% only.
where tailscale >nul 2>&1
if not errorlevel 1 (
  for /f %%I in ('tailscale ip -4 2^>nul') do echo   In Relay enter: %%I:5037
) else (
  echo   In Relay enter this PC's address followed by :5037
)
echo   Unlock the phone and accept the USB debugging prompt. Ctrl+C stops sharing.
echo.
"%ADB%" -a nodaemon server start
goto end

rem ------------------------------------------------------------------ ssh
:ssh
if "%~2"=="" (
  echo Usage: relay-phone.bat ssh user@server [bind]
  exit /b 2
)
set "BIND=%~3"
if not defined BIND set "BIND=172.17.0.1"
"%ADB%" start-server
echo.
if "%BIND%"=="127.0.0.1" (echo   In Relay enter: 127.0.0.1:5037) else (echo   In Relay enter: host.docker.internal:5037)
echo   Unlock the phone and accept the USB debugging prompt. Ctrl+C or closing this window stops sharing.
echo.
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -R %BIND%:5037:127.0.0.1:5037 %~2
if errorlevel 1 (
  echo.
  echo   The tunnel failed. With a bind address other than 127.0.0.1 the server's sshd needs
  echo   "GatewayPorts clientspecified" in /etc/ssh/sshd_config, see docs/RUNNING.md.
  pause
)
goto end

:needadmin
echo Could not add the firewall rule "%RULE%". Run relay-phone.bat once as Administrator
echo (right-click, Run as administrator); later runs do not need it.
pause
exit /b 1

:end
endlocal
