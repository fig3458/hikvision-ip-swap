@echo off
cd /d "%~dp0"
python swap_gui.py
if errorlevel 1 pause
