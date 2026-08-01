#!/bin/sh
# Generate a development internal CA into `.env`, never overwriting one (design §13.4, §14.2).
#
# §13.4 names `scripts/init-ca.sh` and `make init-ca`, so this file exists under that name. The
# work is in `scripts/init_ca.py`, because generating an X.509 CA needs `cryptography` — the same
# pinned library the backend signs device certificates with — and doing it here would mean either
# a second implementation in `openssl` or a dependency on an `openssl` binary that Windows
# developers may not have. §15.8's reasoning applied to a script: one implementation, in the
# language that already owns the format.
#
# The Python is invoked through the backend virtual environment, because that is where the pinned
# `cryptography` is. A bare `python3` would pick up whatever is on PATH and could be a version
# with a different X.509 API.

set -u

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT" || exit 1

for CANDIDATE in backend/.venv/Scripts/python.exe backend/.venv/bin/python python3 python; do
	if command -v "./$CANDIDATE" >/dev/null 2>&1 || [ -x "$CANDIDATE" ]; then
		PYBIN="$CANDIDATE"
		break
	fi
	if command -v "$CANDIDATE" >/dev/null 2>&1; then
		PYBIN="$CANDIDATE"
		break
	fi
done

if [ -z "${PYBIN:-}" ]; then
	printf 'init-ca: FAIL: no Python interpreter found. Run `make bootstrap` first.\n' >&2
	exit 1
fi

exec "$PYBIN" scripts/init_ca.py
