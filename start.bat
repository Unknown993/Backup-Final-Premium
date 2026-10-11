@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
title Premium Guardian Bot
cd /d E:\Premium_Guardian
echo ===================================================
echo             Starting Premium Guardian Bot
echo ===================================================
python bot.py
pause
