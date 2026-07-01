#!/usr/bin/env bash
set -euo pipefail

VENV_DIR=".venv"

# Enforce Python 3.12 to match Dockerfiles (python:3.12-alpine)
if ! command -v python3.12 &>/dev/null; then
  echo "ERROR: python3.12 not found on PATH."
  echo "Install it via pyenv (https://github.com/pyenv/pyenv) or https://www.python.org/downloads/"
  exit 1
fi

echo "Using $(python3.12 --version)"

# Create venv if it doesn't exist (idempotent)
if [ ! -d "$VENV_DIR" ]; then
  echo "Creating $VENV_DIR ..."
  python3.12 -m venv "$VENV_DIR"
fi

# shellcheck source=/dev/null
source "$VENV_DIR/bin/activate"

echo "Upgrading pip..."
pip install --quiet --upgrade pip

echo "Installing service dependencies..."
pip install --quiet -r services/api/requirements.txt
pip install --quiet -r services/event_generator/requirements.txt
pip install --quiet -r services/event_processor/requirements.txt
pip install --quiet -r services/dashboard/requirements.txt

echo "Installing test dependencies..."
pip install --quiet pytest pytest-timeout requests

echo ""
echo "Done. Activate with:"
echo "  source .venv/bin/activate"
echo ""
echo "Then run tests:"
echo "  cd tests && pytest -m unit"
