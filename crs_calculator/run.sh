#!/usr/bin/env bash
# Convenience launcher. Pass --mode cli|telegram|both, defaults to cli.
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -d .venv ]]; then
    python3 -m venv .venv
    .venv/bin/pip install -U pip
    .venv/bin/pip install -r requirements.txt
fi

source .venv/bin/activate
export PYTHONPATH="${PYTHONPATH:-}:$(pwd)"
exec python -m src.main "$@"
