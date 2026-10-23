@echo off
cd /d "%~dp0"
call .venv\Scripts\activate.bat
rem The Stephen-on-Rockall demo: your config.yaml with the character profile on top
radio-agent run -c config.yaml --profile profiles\stephen-rockall.yaml %*
