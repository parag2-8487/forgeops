#!/usr/bin/env bash
# SPDX-License-Identifier: FSL-1.1-ALv2
#
# Refuse a backend test run whose backing services are down.
#
# WHY THIS EXISTS: A SKIPPED SUITE READS LIKE A PASSING ONE
#
# The DB-backed property tests skip when Postgres is unreachable, which is correct on its own — a
# developer without Docker should still be able to run the unit suite. The problem is what that does
# to the MUTATION HARNESS, which `tests/meta/test_regime_end_to_end.py` runs over the real tree.
#
# The harness applies a mutation and asserts the corresponding property test FAILS. When the property
# test skips instead, the harness reports `ERROR: the control's run executed no tests (all skipped or
# none collected)` — it is being honest, and that honesty is the whole point of Appendix B. But
# `make test` then exits non-zero with a wall of output in which the real message is that Q-04, Q-16
# and Q-17 were never exercised at all, and Q-05 passed under its own negative control.
#
# That happened. Diagnosing it took a full 66-minute run followed by a second one to confirm the
# containers were the cause and not the code. Worse is the other direction: a suite where whole
# control classes silently skipped could be read as green by anyone not looking closely.
#
# So this fails FAST and names the missing containers, before an hour of test time is spent.
#
# WHAT IT DOES NOT DO: it does not start anything. Starting containers as a side effect of `make
# test` would make the test command mutate the developer's environment, and a run that quietly
# started a database is a run whose result depends on state the developer did not choose.

set -u

REQUIRED_CONTAINERS="forgeops-test-pg forgeops-test-redis forgeops-test-cerbos"

if ! command -v docker >/dev/null 2>&1; then
    cat >&2 <<'EOF'
==> preflight: docker is not on PATH.

The backend test suite needs Postgres, Redis and Cerbos. Without them the DB-backed property tests
SKIP, and a skipped negative control is indistinguishable from a passing one in the summary line --
which is exactly the false green this check exists to prevent.

Install Docker, then: make test-services-up
EOF
    exit 1
fi

missing=""
for name in ${REQUIRED_CONTAINERS}; do
    state="$(docker inspect -f '{{.State.Running}}' "${name}" 2>/dev/null || echo absent)"
    if [ "${state}" != "true" ]; then
        missing="${missing} ${name}(${state})"
    fi
done

if [ -n "${missing}" ]; then
    cat >&2 <<EOF
==> preflight: REFUSING to run the backend suite. These containers are not running:
   ${missing}

WHY THIS IS AN ERROR AND NOT A WARNING. The DB-backed property tests skip without them, and the
mutation harness then reports that Q-04, Q-16 and Q-17 executed no tests and that Q-05 passed under
its own negative control. Those are honest reports of a vacuous run, but they arrive 66 minutes in,
and a summary line reading "1873 passed, 552 skipped" looks like success.

Start them and re-run:
   docker start ${REQUIRED_CONTAINERS}

Or, if they have never existed on this machine, see docs/development.md for the test stack.
EOF
    exit 1
fi

echo "==> preflight: ${REQUIRED_CONTAINERS} are running"
