#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi

source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -r requirements-build.txt

echo "VENV ready: $(pwd)/.venv"
echo "Run service: ./run.sh"
echo "Build package: ./build_package.sh"
