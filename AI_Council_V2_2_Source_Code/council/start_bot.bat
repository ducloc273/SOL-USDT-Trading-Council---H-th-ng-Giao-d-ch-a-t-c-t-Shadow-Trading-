@echo off
setlocal
cd /d "%~dp0"
set "AUTOSHADOW=1"
:loop
py telegram_bot.py
if errorlevel 1 echo Bot stopped with error.
echo Restarting in 30 seconds. Press Ctrl+C to stop.
timeout /t 30 /nobreak
goto loop
