#!/usr/bin/env sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
python_bin="$repo_root/.venv/bin/python"
if [ ! -x "$python_bin" ]; then
  echo "Expected the repository Python 3.12 virtual environment at $repo_root/.venv" >&2
  exit 1
fi
python_version=$($python_bin -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
if [ "$python_version" != "3.12" ]; then
  echo "OpenAPI generation requires the Python 3.12 virtual environment (found $python_version)" >&2
  exit 1
fi

cd "$repo_root"
PYTHONPATH=src "$python_bin" scripts/export_openapi.py frontend/openapi.json
cd frontend
npx openapi-typescript openapi.json -o src/api/schema.d.ts
