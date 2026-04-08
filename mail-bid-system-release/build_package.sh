#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo ".venv not found. Run ./setup_venv.sh first."
  exit 1
fi

source .venv/bin/activate
python -m pip install -r requirements-build.txt

rm -rf build dist release
mkdir -p release

pyinstaller \
  --name mail-bid-system \
  --onedir \
  --clean \
  --add-data "templates:templates" \
  --add-data "static:static" \
  --add-data "README.md:." \
  app.py

cp run.sh release/ || true
cp run.bat release/ || true
cp README_CN.md release/
mkdir -p release/mailbox/Outbox release/uploads release/data
cp -R dist/mail-bid-system release/

cd release
zip -rq mail-bid-system-macos.zip mail-bid-system README_CN.md run.sh run.bat mailbox uploads data

echo "Package done: $(pwd)/mail-bid-system-macos.zip"
