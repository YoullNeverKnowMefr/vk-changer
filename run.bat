@echo off
REM Runs the mirror loop and restarts it if the process ever exits/crashes.
REM For 24/7 operation on a Windows server, point a Scheduled Task
REM ("At startup", run whether user is logged on or not) at this file,
REM or wrap it with NSSM (https://nssm.cc) as a Windows service:
REM   nssm install VKMirror "C:\path\to\run.bat"
cd /d "%~dp0"

:loop
python vk_mirror.py
echo [run.bat] vk_mirror.py exited with code %ERRORLEVEL%, restarting in 30s...
timeout /t 30 /nobreak >nul
goto loop
