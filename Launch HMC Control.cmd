@echo off
cd /d "%~dp0"
python hmc_gui.py %*
if errorlevel 1 pause
