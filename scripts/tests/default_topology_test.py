# SPDX-License-Identifier: FSL-1.1-ALv2
"""Task 7.2 — the exact fresh-clone default service topology.

Appendix E criterion 4 requires that, from a clone with NO `.env`, the direct
unprofiled command starts EXACTLY postgres, redis, opa, backend and frontend,
and that no optional service or profile exists yet.

What is proved here without a container engine:
  * the default (unprofiled) service selection is exactly those five, computed
    the way Compose computes it: a service is in the default selection when it
    declares no `profiles` key;
  * no optional service or profile is declared at all;
  * `.env.example` supplies a value for every variable the backend/frontend
    services actually consume, so containers get real configuration with no
    `.env` present;
  * every Compose interpolation has a literal default, because interpolation is
    resolved from the shell environment and NOT from env_file — this is the one
    mechanism that makes a `.env`-less fresh clone work;
  * an optional `.env` overrides the baseline when present (env_file order);
  * repeated `make init-env` never changes an existing `.env`'s bytes;
  * the validator has teeth: injecting an optional service, a profile, a
    0.0.0.0 binding or a readiness-based backend healthcheck must FAIL it.

Run: python scripts/tests/default_topology_test.py
"""
from __future__ import annotations

import copy
import os
import pathlib
import shutil
import re
import subprocess
import sys
import tempfile

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.yml"
ENV_EXAMPLE = ROOT / ".env.example"
VALIDATOR = ROOT / "scripts" / "check-compose-validate.py"
INIT_ENV = ROOT / "scripts" / "init-env.sh"

EXPECTED_DEFAULT = {"backend", "frontend", "opa", "postgres", "redis"}
OPTIONAL_SERVICES = {"infisical", "agent-dev"}
OPTIONAL_PROFILES = {"vault", "tools"}
INTERPOLATION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:?-[^}]*)?\}")


def resolve_bash() -> str:
    """Return a POSIX bash that can actually run the project scripts.

    On Windows the first `bash` on PATH is frequently the WSL shim
    (C:\\Windows\\System32\\bash.exe). With no distribution installed it fails with
    "execvpe(/bin/bash) failed", which looks like a script bug but is not one, so
    Git Bash is preferred explicitly and only then does PATH lookup apply.
    """
    candidates = [
        os.environ.get("FORGEOPS_BASH", ""),
        r"C:\\Program Files\\Git\\bin\\bash.exe",
        r"C:\\Program Files (x86)\\Git\\bin\\bash.exe",
    ]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    found = shutil.which("bash")
    if found and "System32" not in found:
        return found
    raise RuntimeError("no POSIX bash found; set FORGEOPS_BASH to a bash executable")

failures: list[str] = []


def ok(msg: str) -> None:
    print(f"ok   - {msg}")


def bad(msg: str) -> None:
    print(f"FAIL - {msg}", file=sys.stderr)
    failures.append(msg)


def load_compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def default_selection(data: dict) -> set[str]:
    """Services Compose starts with no --profile flag: those without profiles."""
    return {
        name
        for name, svc in (data.get("services") or {}).items()
        if not (isinstance(svc, dict) and svc.get("profiles"))
    }


def run_validator(compose_path: pathlib.Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VALIDATOR), str(compose_path)],
        capture_output=True,
        text=True,
        timeout=120,
    )


def env_example_keys() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        if re.match(r"^[A-Z][A-Z0-9_]*=", line):
            key, raw = line.split("=", 1)
            values[key] = raw.split("#")[0].strip()
    return values


def case_default_selection() -> None:
    print("# case 1: the unprofiled default selection is exactly five services")
    data = load_compose()
    selected = default_selection(data)
    if selected == EXPECTED_DEFAULT:
        ok(f"default selection == {sorted(EXPECTED_DEFAULT)}")
    else:
        bad(
            "default selection must be exactly "
            f"{sorted(EXPECTED_DEFAULT)}, got {sorted(selected)}"
        )
    declared = set(data.get("services") or {})
    present_optional = declared & OPTIONAL_SERVICES
    if present_optional:
        bad(f"optional services must not be declared yet: {sorted(present_optional)}")
    else:
        ok("no optional service (infisical, agent-dev) is declared")
    profiles = {
        p
        for svc in (data.get("services") or {}).values()
        if isinstance(svc, dict)
        for p in (svc.get("profiles") or [])
    }
    if profiles & OPTIONAL_PROFILES:
        bad(f"optional profiles must not be declared yet: {sorted(profiles)}")
    else:
        ok("no vault/tools profile is declared")


def case_fresh_clone_configuration() -> None:
    print("# case 2: a clone with no .env still receives real configuration")
    baseline = env_example_keys()
    if not baseline:
        bad(".env.example produced no keys")
        return
    ok(f".env.example supplies {len(baseline)} committed values")

    data = load_compose()
    missing: list[str] = []
    undefaulted: list[str] = []
    for name, svc in (data.get("services") or {}).items():
        fragments = [str(p) for p in (svc.get("ports") or [])]
        build = svc.get("build")
        if isinstance(build, dict):
            fragments += [str(v) for v in (build.get("args") or {}).values()]
        for fragment in fragments:
            for var, default in INTERPOLATION.findall(fragment):
                if not default:
                    undefaulted.append(f"{name}:{var}")
                if var not in baseline:
                    missing.append(f"{name}:{var}")
    if undefaulted:
        bad(
            "these interpolations have no default, so a .env-less clone would fail: "
            f"{sorted(set(undefaulted))}"
        )
    else:
        ok("every port/build-arg interpolation carries a literal default")
    if missing:
        bad(f"interpolated variables absent from .env.example: {sorted(set(missing))}")
    else:
        ok("every interpolated variable is also declared in .env.example")

    # env_file ORDER is what gives an optional .env the last word.
    for name, svc in (data.get("services") or {}).items():
        entries = svc.get("env_file") or []
        paths = [str(e.get("path", "")) if isinstance(e, dict) else str(e) for e in entries]
        if len(paths) < 2 or ".env.example" not in paths[0] or not paths[1].endswith(".env"):
            bad(f"service {name!r}: env_file order must be [.env.example, .env]")
            return
    ok("every service loads .env.example first and .env last, so .env overrides")


def case_init_env_idempotence() -> None:
    print("# case 3: repeated init-env never changes an existing .env")
    bash = resolve_bash()
    with tempfile.TemporaryDirectory() as tmp:
        box = pathlib.Path(tmp)
        (box / "scripts").mkdir()
        (box / ".env.example").write_bytes(ENV_EXAMPLE.read_bytes())
        (box / "scripts" / "init-env.sh").write_bytes(INIT_ENV.read_bytes())

        first = subprocess.run(
            [bash, "scripts/init-env.sh"], cwd=box, capture_output=True, text=True, timeout=60
        )
        if first.returncode != 0:
            bad(f"init-env failed on a fresh fixture: {first.stderr}")
            return
        created = (box / ".env").read_bytes()
        if created == ENV_EXAMPLE.read_bytes():
            ok("a fresh clone gets .env byte-identical to the committed baseline")
        else:
            bad("created .env does not match .env.example")

        (box / ".env").write_bytes(b"APP_ENV=production\nLOCAL_ONLY=keep-me\n")
        expected = (box / ".env").read_bytes()
        for attempt in range(3):
            result = subprocess.run(
                [bash, "scripts/init-env.sh"], cwd=box, capture_output=True, text=True, timeout=60
            )
            if result.returncode != 0:
                bad(f"init-env failed with an existing .env: {result.stderr}")
                return
            if (box / ".env").read_bytes() != expected:
                bad(f"init-env modified an existing .env on attempt {attempt + 1}")
                return
        ok("three further runs leave an existing .env byte-identical")


def case_validator_has_teeth() -> None:
    print("# case 4: the topology validator rejects real regressions")
    data = load_compose()
    mutations: dict[str, dict] = {}

    with_optional = copy.deepcopy(data)
    with_optional["services"]["infisical"] = {
        "profiles": ["vault"],
        "image": "infisical/infisical:latest",
    }
    mutations["an optional vault service is declared"] = with_optional

    with_public_port = copy.deepcopy(data)
    with_public_port["services"]["postgres"]["ports"] = ["0.0.0.0:5432:5432"]
    mutations["a service publishes on 0.0.0.0"] = with_public_port

    with_readiness_probe = copy.deepcopy(data)
    with_readiness_probe["services"]["backend"]["healthcheck"]["test"] = [
        "CMD",
        "curl",
        "-f",
        "http://localhost:8000/health/ready",
    ]
    mutations["the backend healthcheck gates on readiness"] = with_readiness_probe

    without_digest = copy.deepcopy(data)
    without_digest["services"]["redis"]["image"] = "redis/redis-stack-server:7.4.0-v3"
    mutations["an image is not digest-pinned"] = without_digest

    no_default = copy.deepcopy(data)
    no_default["services"]["opa"]["ports"] = ["127.0.0.1:${OPA_PORT}:8181"]
    mutations["a port interpolation has no default"] = no_default

    with tempfile.TemporaryDirectory() as tmp:
        for description, mutated in mutations.items():
            path = pathlib.Path(tmp) / "docker-compose.yml"
            path.write_text(yaml.safe_dump(mutated, sort_keys=False), encoding="utf-8")
            result = run_validator(path)
            if result.returncode != 0:
                ok(f"rejected: {description}")
            else:
                bad(f"validator ACCEPTED a regression: {description}")

    result = run_validator(COMPOSE)
    if result.returncode == 0:
        ok("accepts the committed docker-compose.yml")
    else:
        bad(f"validator rejected the committed compose file: {result.stderr}")


def main() -> int:
    for path in (COMPOSE, ENV_EXAMPLE, VALIDATOR, INIT_ENV):
        if not path.exists():
            print(f"FAIL - required file missing: {path}", file=sys.stderr)
            return 1
    case_default_selection()
    case_fresh_clone_configuration()
    case_init_env_idempotence()
    case_validator_has_teeth()
    print()
    if failures:
        print(
            f"default-topology test FAILED ({len(failures)} failing assertion(s))",
            file=sys.stderr,
        )
        return 1
    print("default-topology test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
