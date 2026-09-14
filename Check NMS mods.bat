@echo off
rem Double-click to check every No Man's Sky mod and open the report.
cd /d "%~dp0"
python nms_mod_check.py %*
if errorlevel 1 pause
