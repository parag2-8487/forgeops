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
./.venv/Scripts/python.exe -m pytest "$@" >"$log" 2>&1
status=$?
set -e
echo "pytest exit=$status  log=$log"
tail -n "${FORGEOPS_PYTEST_TAIL:-30}" "$log"
exit "$status"
