#!/usr/bin/env bash
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
source "$DIR/.venv/bin/activate"
exec python -m workflow.ws "$@"