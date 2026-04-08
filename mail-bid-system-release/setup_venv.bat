@echo off
setlocal
cd /d %~dp0

if not exist .venv (
  py -3 -m venv .venv
)

call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -r requirements-build.txt

echo.
echo VENV ready: %cd%\.venv
echo Run service: run.bat
echo Build package: build_package.bat
pause
