#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo ".venv not found. Run ./setup_venv.sh first."
  exit 1
fi

source .venv/bin/activate
python app.py
