@echo off
title Volta Radar
cd /d "%~dp0"
:loop
python notifier.py
echo Bot stopped, restarting in 10 seconds...
timeout /t 10 >nul
goto loop
