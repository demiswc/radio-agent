@echo off
cd /d "%~dp0"
if not exist .venv (
  py -m venv .venv 2>nul || python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -e .[radio,whisper]
echo.
echo === Sound devices (find your mic-in / line-in number) ===
radio-agent devices
