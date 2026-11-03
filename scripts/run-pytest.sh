#!/bin/sh
# Run the backend mandatory selection and keep both streams in one readable log.
#
# Exists because PowerShell 5.1 surfaces only the first stderr line of a native command as a
# NativeCommandError and swallows the rest, so a pytest run that fails at startup looks like a
# run that produced no output at all. Redirecting inside a shell keeps the whole thing.
#
# Usage, from the repository root, with scripts/local-env.ps1 already dot-sourced:
#   & 'C:\Program Files\Git\bin\bash.exe' scripts/run-pytest.sh -m mandatory -q -p no:randomly
set -eu
root=$(cd "$(dirname "$0")/.." && pwd)
log=${FORGEOPS_PYTEST_LOG:-/tmp/forgeops-pytest.log}
cd "$root/backend"
set +e
if [ -x "./.venv/bin/python" ]; then
  PY_BIN="./.venv/bin/python"
elif [ -x "./.venv/Scripts/python.exe" ]; then
  PY_BIN="./.venv/Scripts/python.exe"
elif [ -x "./.venv/Scripts/python" ]; then
  PY_BIN="./.venv/Scripts/python"
elif command -v python3 >/dev/null 2>&1; then
  PY_BIN="python3"
else
  PY_BIN="python"
fi
"$PY_BIN" -m pytest "$@" >"$log" 2>&1
status=$?
set -e
echo "pytest exit=$status  log=$log"
tail -n "${FORGEOPS_PYTEST_TAIL:-30}" "$log"
exit "$status"
