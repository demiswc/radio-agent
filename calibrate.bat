@echo off
cd /d "%~dp0"
call .venv\Scripts\activate.bat
radio-agent calibrate -c config.yaml
