#!/usr/bin/env python3
"""Static docker-compose.yml validator for the Phase 0 DEFAULT profile.

design.md §2.2 and Appendix E criterion 4 fix the unprofiled default set at
exactly five services — postgres, redis, opa, backend, frontend. §13.3 fixes the
rest of the shape. This validator asserts all of that statically, so it holds on
a machine without a container engine:

  * project name `forgeops`;
  * the UNPROFILED set is EXACTLY the five default services — any other service
    must declare a profile, so it cannot be pulled in by a bare
    `docker compose up`;
  * the optional services are profile-gated to their owning task's profile:
    `infisical` → `vault` (task 13.5), `agent-dev` → `tools` (task 9.5). Before
    those tasks landed the services were absent entirely; now that they exist,
    the invariant that matters is that they stay OUT of the default selection;
  * no default service declares a profile (that would remove it from the
    unprofiled set and silently break criterion 4);
  * image-based services are pinned by tag AND @sha256: digest;
  * no service overrides `user:` back to uid 0, which would defeat an image that
    already drops privilege (debt D5 as corrected by D-51 — the retired rule
    matched a `-rootless` substring in a tag name, which proves a naming
    convention rather than a runtime user);
  * build-based services declare an explicit build target;
  * every published port binds to 127.0.0.1 only (§14.2);
  * `.env.example` is a REQUIRED env file and `.env` an OPTIONAL override for
    every service, which is what makes a fresh clone with no `.env` start;
  * every `${VAR}` interpolation in ports/build args carries a safe default,
    because Compose interpolation does not read env_file;
  * the backend container healthcheck is LIVENESS only — it must probe /health
    and must NOT probe /health/ready, since readiness is gated separately by
    scripts/dev-up.sh (§4.4, §13.3);
  * the frontend passes both NEXT_PUBLIC_* values as BUILD ARGS with
    browser-reachable defaults (§12.6) and waits for the backend to be healthy;
  * the named volumes pgdata and redisdata exist.

Usage: check-compose-validate.py <docker-compose.yml>
"""
from __future__ import annotations

import pathlib
import re
import sys

try:
    import yaml
except ImportError:  # pragma: no cover
    print("FAIL: PyYAML is required. Install with: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

#: The default set is READ from `scripts/compose-default-services.txt`, not restated
#: here. That file's own header already claimed it was "Read by the `compose-smoke` CI
#: job and by scripts/check-compose-validate.py" — and it was not: this module carried
#: its own literal set, so the two could drift and the file documented a coupling it did
#: not have. Task 6.3 made that concrete, because promoting the two Authentik rows in the
#: data file changed nothing here. One source, parsed once.
def _load_default_services() -> set[str]:
    """Service names from the committed data file, ignoring comments and blanks.

    A `# pending:` line is a service design §2.3 requires that has not landed yet; it is
    a comment, so it is not in the set — which is the whole point of the marker.

    A missing or empty file is a hard failure rather than an empty set: an empty expected
    set would make the "unprofiled set must be exactly this" assertion pass for any
    compose file at all, which is the vacuity this check exists to avoid.
    """
    path = pathlib.Path(__file__).resolve().parent / "compose-default-services.txt"
    if not path.is_file():
        raise SystemExit(f"FAIL: {path} is missing; it is the source of the default set")
    names = {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    if not names:
        raise SystemExit(f"FAIL: {path} lists no services; an empty expected set proves nothing")
    return names


DEFAULT_SERVICES = _load_default_services()
# Optional services and the profile each MUST be gated behind. They were absent
# until their owning task landed (agent-dev in 9.5, infisical in 13.5); now the
# invariant is that they stay out of the unprofiled default selection.
OPTIONAL_SERVICE_PROFILES = {"infisical": "vault", "agent-dev": "tools"}
BUILD_SERVICES = {"backend", "frontend"}
#: Derived, so a new default service is covered without editing a second list.
IMAGE_SERVICES = DEFAULT_SERVICES - BUILD_SERVICES
#: Every default service must declare a healthcheck. This was previously
#: {postgres, redis, opa, backend}, excusing the frontend "because depends_on gates it" —
#: but task 2.3 (debt D2) had already given the frontend a healthcheck precisely because
#: `up -d --wait` waits only for *running* otherwise. The exemption was therefore already
#: obsolete, and Authentik makes it actively wrong: its first boot runs its own migrations
#: for minutes, so a stack that reports ready before `ak healthcheck` passes reports an
#: IdP that cannot yet serve a login.
HEALTHCHECK_SERVICES = set(DEFAULT_SERVICES)
PUBLIC_BUILD_ARGS = {"NEXT_PUBLIC_API_BASE_URL", "NEXT_PUBLIC_APP_NAME"}

INTERPOLATION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:?-[^}]*)?\}")

#: An unresolved digest placeholder. Design §13.3 writes image references with
#: `<committed-digest>` where the real digest belongs, and a copy of that text
#: reaching docker-compose.yml would be an image reference that cannot be pulled —
#: failing at `docker compose up` rather than at review, and looking like a pinned
#: reference in a diff.
PLACEHOLDER_DIGEST = re.compile(r"<[^>]*digest[^>]*>|@sha256:<|@sha256:\s*$|@sha256:(?:x|X|0){6,}")

#: A `user:` override that puts the container back on uid 0. Compose accepts
#: `root`, `0`, `0:0`, `root:root` and `0:root`; all of them defeat an image whose
#: own Dockerfile dropped privilege. Design §0.5 debt D5, as corrected by D-51:
#: the original gate matched a `-rootless` substring in a tag name, which proves a
#: naming convention rather than a runtime user — a `-rootless` image reconfigured
#: with `user: root` would have sailed through it. This rule checks the thing that
#: actually decides the runtime uid in Compose, and `compose-smoke` proves the
#: remaining half by reading `id -u` out of the running container.
ROOT_USER = re.compile(r"^\s*(?:0|root)\s*(?::\s*(?:0|root)\s*)?$")


def _uid_is_root(value: object) -> bool:
    return bool(ROOT_USER.match(str(value)))


def check(compose_path: str) -> list[str]:
    with open(compose_path, encoding="utf-8") as fh:
        content = fh.read()

    data = yaml.safe_load(content)
    if not isinstance(data, dict):
        return ["docker-compose.yml did not parse as a mapping"]

    errors: list[str] = []

    if data.get("name") != "forgeops":
        errors.append(f"project name must be 'forgeops', got {data.get('name')!r}")

    services = data.get("services")
    if not isinstance(services, dict):
        return errors + ["'services' is missing or not a mapping"]

    actual = set(services)

    # The set a bare `docker compose up` selects is every service with NO profile.
    unprofiled = {
        name
        for name, svc in services.items()
        if isinstance(svc, dict) and not (svc.get("profiles") or [])
    }
    if unprofiled != DEFAULT_SERVICES:
        missing = sorted(DEFAULT_SERVICES - unprofiled)
        extra = sorted(unprofiled - DEFAULT_SERVICES)
        errors.append(
            "the unprofiled default set must be exactly "
            f"{sorted(DEFAULT_SERVICES)}; missing={missing} unexpected={extra}"
        )

    # Anything beyond the five must be a known optional service behind its profile.
    for name in sorted(actual - DEFAULT_SERVICES):
        if name not in OPTIONAL_SERVICE_PROFILES:
            errors.append(
                f"unexpected service {name!r}: only "
                f"{sorted(OPTIONAL_SERVICE_PROFILES)} may exist beyond the default set"
            )
            continue
        expected = OPTIONAL_SERVICE_PROFILES[name]
        declared = list((services[name] or {}).get("profiles") or [])
        if declared != [expected]:
            errors.append(
                f"optional service {name!r} must declare exactly "
                f"profiles: [{expected!r}], got {declared!r}"
            )

    for name, svc in services.items():
        if not isinstance(svc, dict):
            errors.append(f"service {name!r} is not a mapping")
            continue

        # A default service must never carry a profile: that would drop it out of
        # the unprofiled selection and silently break criterion 4.
        if name in DEFAULT_SERVICES and (svc.get("profiles") or []):
            errors.append(
                f"default service {name!r} must not declare a profile, got "
                f"{svc.get('profiles')!r}"
            )

        # --- image / build pinning -------------------------------------------
        # Debt D5 (design §0.5). This rule used to apply only to IMAGE_SERVICES
        # (postgres, redis, opa), which is exactly how `infisical/infisical:v0.91.1`
        # sat unpinned while every other image carried a digest: the optional
        # profile was outside the set the check looked at. The rule is now universal
        # — ANY service that names an image must pin it — so a new service cannot
        # arrive unpinned by virtue of not being on a list.
        image = svc.get("image", "")
        if image:
            if PLACEHOLDER_DIGEST.search(image):
                errors.append(
                    f"service {name!r}: image carries an unresolved digest "
                    f"placeholder, got {image!r}. Resolve it with "
                    f"`docker buildx imagetools inspect <ref> --format "
                    f"'{{{{.Manifest.Digest}}}}'` and commit the real digest."
                )
            elif "@sha256:" not in image:
                errors.append(
                    f"service {name!r}: image must be digest-pinned, got {image!r}"
                )
            if ":" not in image.split("@")[0]:
                errors.append(
                    f"service {name!r}: image must carry an explicit tag as well as "
                    f"a digest, got {image!r}"
                )
        if name in IMAGE_SERVICES and not image:
            errors.append(f"service {name!r}: expected an image reference, found none")

        # --- no service may climb back to uid 0 -----------------------------
        # Debt D5, as corrected by D-51. Every image in the topology is non-root at
        # rest (OPA is `1000:1000` on a Chainguard base; the two built images set
        # their own USER), so the only way a container in this file ends up root is
        # an explicit override here.
        if "user" in svc and _uid_is_root(svc.get("user")):
            errors.append(
                f"service {name!r}: user override {svc.get('user')!r} puts the "
                f"container back on uid 0, defeating an image that already drops "
                f"privilege (design §0.5 debt D5, §17.1 D-51)"
            )
        if name in BUILD_SERVICES:
            build = svc.get("build")
            if not isinstance(build, dict):
                errors.append(f"service {name!r}: build must be a mapping")
            elif build.get("target") != "runtime":
                errors.append(
                    f"service {name!r}: build.target must be 'runtime', got "
                    f"{build.get('target')!r}"
                )

        # --- loopback-only publishing ---------------------------------------
        for port in svc.get("ports") or []:
            port_str = str(port)
            if not port_str.startswith("127.0.0.1:"):
                errors.append(
                    f"service {name!r}: published port must bind 127.0.0.1 only "
                    f"(design §14.2), got {port_str!r}"
                )
            for var, default in INTERPOLATION.findall(port_str):
                if not default:
                    errors.append(
                        f"service {name!r}: port interpolation ${{{var}}} has no "
                        "default; Compose interpolation does not read env_file, so a "
                        "fresh clone without .env would fail"
                    )

        # --- env_file semantics ---------------------------------------------
        env_file = svc.get("env_file")
        if not isinstance(env_file, list) or len(env_file) < 2:
            errors.append(
                f"service {name!r}: env_file must list the required .env.example "
                "baseline and the optional .env override"
            )
        else:
            baseline, override = env_file[0], env_file[1]
            if not isinstance(baseline, dict) or ".env.example" not in str(
                baseline.get("path", "")
            ):
                errors.append(
                    f"service {name!r}: first env_file entry must be ./.env.example"
                )
            elif baseline.get("required") is not True:
                errors.append(
                    f"service {name!r}: ./.env.example must be required: true"
                )
            override_path = str(override.get("path", "")) if isinstance(override, dict) else ""
            if not override_path.endswith(".env"):
                errors.append(
                    f"service {name!r}: second env_file entry must be ./.env"
                )
            elif override.get("required") is not False:
                errors.append(f"service {name!r}: ./.env must be required: false")

        # --- healthchecks ----------------------------------------------------
        healthcheck = svc.get("healthcheck")
        if name in HEALTHCHECK_SERVICES:
            if not isinstance(healthcheck, dict) or "test" not in healthcheck:
                errors.append(f"service {name!r}: healthcheck.test is missing")
            elif name == "backend":
                probe = " ".join(str(x) for x in healthcheck["test"])
                if "/health/ready" in probe:
                    errors.append(
                        "the backend container healthcheck must be LIVENESS only; "
                        "readiness is gated by scripts/dev-up.sh (design §4.4, §13.3)"
                    )
                if "/health" not in probe:
                    errors.append(
                        "the backend container healthcheck must probe /health"
                    )

    # --- frontend build-time public environment (design §12.6) --------------
    frontend = services.get("frontend")
    if isinstance(frontend, dict):
        build = frontend.get("build")
        args = build.get("args") if isinstance(build, dict) else None
        if not isinstance(args, dict):
            errors.append(
                "service 'frontend': build.args must supply the NEXT_PUBLIC_* values, "
                "because they are inlined at build time"
            )
        else:
            for key in PUBLIC_BUILD_ARGS:
                if key not in args:
                    errors.append(f"service 'frontend': build.args is missing {key}")
                    continue
                value = str(args[key])
                for var, default in INTERPOLATION.findall(value):
                    if not default:
                        errors.append(
                            f"service 'frontend': build arg {key} interpolation "
                            f"${{{var}}} has no default"
                        )
            api = str(args.get("NEXT_PUBLIC_API_BASE_URL", ""))
            if "backend:" in api:
                errors.append(
                    "service 'frontend': NEXT_PUBLIC_API_BASE_URL must be a "
                    "BROWSER-reachable URL, not the server-internal hostname"
                )
        depends = frontend.get("depends_on")
        if not isinstance(depends, dict) or depends.get("backend", {}).get(
            "condition"
        ) != "service_healthy":
            errors.append(
                "service 'frontend': must depend on backend with condition "
                "service_healthy (design §13.3)"
            )

    volumes = data.get("volumes")
    if not isinstance(volumes, dict):
        errors.append("'volumes' is missing or not a mapping")
    else:
        for volume in ("pgdata", "redisdata"):
            if volume not in volumes:
                errors.append(f"named volume {volume!r} is missing")

    return errors


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: check-compose-validate.py <docker-compose.yml>", file=sys.stderr)
        return 1

    errors = check(sys.argv[1])
    if errors:
        print("FAIL: docker-compose.yml default-profile validation failed:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print("OK: docker-compose.yml passes every Phase 0 default-profile assertion")
    for line in (
        "project name: forgeops",
        f"default services (unprofiled): {sorted(DEFAULT_SERVICES)}",
        "optional services: infisical behind 'vault', agent-dev behind 'tools' — "
        "declared but excluded from the unprofiled set",
        "image services: tag + @sha256: digest pinned",
        "no service overrides user: back to uid 0 (D-51)",
        "build services: explicit runtime target",
        "published ports: 127.0.0.1 only, all interpolations defaulted",
        "env_file: ./.env.example required, ./.env optional",
        "backend healthcheck: liveness only (/health, never /health/ready)",
        "frontend: NEXT_PUBLIC_* passed as build args with browser-reachable defaults",
        "named volumes: pgdata, redisdata",
    ):
        print(f"  - {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
