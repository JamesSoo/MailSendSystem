@echo off
setlocal
cd /d %~dp0

if not exist .venv\Scripts\python.exe (
  echo .venv not found. Run setup_venv.bat first.
  exit /b 1
)

call .venv\Scripts\activate.bat
python -m pip install -r requirements-build.txt

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist release rmdir /s /q release
mkdir release

pyinstaller --name mail-bid-system --onedir --clean --add-data "templates;templates" --add-data "static;static" --add-data "README_CN.md;." app.py

copy README_CN.md release\ >nul
copy run.bat release\ >nul
copy run.sh release\ >nul
mkdir release\mailbox\Outbox
mkdir release\uploads
mkdir release\data
xcopy /E /I /Y dist\mail-bid-system release\mail-bid-system >nul

powershell -NoProfile -Command "Compress-Archive -Path 'release\\mail-bid-system','release\\README_CN.md','release\\run.bat','release\\run.sh','release\\mailbox','release\\uploads','release\\data' -DestinationPath 'release\\mail-bid-system-windows.zip' -Force"

echo Package done: %cd%\release\mail-bid-system-windows.zip
