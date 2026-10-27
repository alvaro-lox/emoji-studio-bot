@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
title Deploy Emoji Studio

where py >nul 2>nul || (echo Python не найден. Установи его с python.org & pause & exit /b 1)

echo Проверяю библиотеку для SSH...
py -m pip install -q --disable-pip-version-check paramiko

py deploy.py
echo.
pause
