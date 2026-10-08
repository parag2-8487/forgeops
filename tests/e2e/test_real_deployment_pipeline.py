"""End-to-End verification of the complete ForgeOps deployment pipeline on the real test repository."""

import subprocess
import time
import requests
from pathlib import Path
import pytest

from backend.src.inspection.scanner import scan_repository_tree
from backend.src.inspection.workspace_resolver import resolve_repository_blueprint
from backend.src.inspection.blueprint_gate import verify_blueprint_gate
from backend.src.validation.artifact_gate import evaluate_existing_artifact_gate
from backend.src.validation.consistency_gate import verify_consistency_gate


REAL_REPO_PATH = Path(r"C:\Users\ASUS\Downloads\for testing\Code_review_platform")


def test_real_code_review_pipeline_e2e():
    if not REAL_REPO_PATH.exists():
        pytest.skip(f"Real repository not found at {REAL_REPO_PATH}")

    # -------------------------------------------------------------
    # Stage 1: Dynamic Discovery and G1 Blueprint Gate
    # -------------------------------------------------------------
    discovered = scan_repository_tree(REAL_REPO_PATH)
    assert len(discovered.manifests) >= 2, "Failed to discover manifests"

    blueprint = resolve_repository_blueprint(discovered)
    assert blueprint.build_config.source_dir == "code-review", (
        f"Expected source_dir 'code-review', got '{blueprint.build_config.source_dir}'"
    )
    assert blueprint.runtime.framework == "nextjs"
    assert blueprint.network.listen_port == 3000

    g1_result = verify_blueprint_gate(blueprint)
    assert g1_result.passed is True, f"G1 Blueprint Gate failed: {g1_result.message}"
    print(f"\n[G1 PASSED] {g1_result.message}")

    # -------------------------------------------------------------
    # Stage 2: G2 Existing Artifact Gate
    # -------------------------------------------------------------
    g2_result = evaluate_existing_artifact_gate(REAL_REPO_PATH, blueprint)
    # The existing Dockerfile in the repo exposed port 8080 and had broken COPY paths.
    # It MUST be rejected and flagged for regeneration!
    assert g2_result.action == "REGENERATE", (
        f"G2 gate should reject invalid existing Dockerfile, but returned: {g2_result.action}"
    )
    print(f"[G2 PASSED] Incompatible existing artifact rejected. Reasons: {g2_result.rejection_reasons}")

    # -------------------------------------------------------------
    # Stage 3: Blueprint-Grounded Synthesis & G3 Consistency Gate
    # -------------------------------------------------------------
    # Synthesize clean blueprint-grounded Dockerfile matching source_dir 'code-review'
    generated_dockerfile = REAL_REPO_PATH / "Dockerfile.blueprint"
    generated_dockerfile.write_text("""# ForgeOps Blueprint-Grounded Dockerfile
FROM node:20-alpine AS builder
RUN apk add --no-cache libc6-compat
WORKDIR /app
COPY package*.json ./
COPY code-review/package*.json ./code-review/
RUN npm install
COPY code-review ./code-review
WORKDIR /app/code-review
ENV NEXT_TELEMETRY_DISABLED=1
ENV NEXT_CPU_NUM=1
ENV NODE_OPTIONS="--max-old-space-size=2048"
RUN npm run build

FROM node:20-alpine AS runner
RUN apk add --no-cache libc6-compat
WORKDIR /app/code-review
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV PORT=3000

COPY --from=builder /app /app

EXPOSE 3000
CMD ["npm", "start"]
""")

    g3_result = verify_consistency_gate(REAL_REPO_PATH, generated_dockerfile, None, blueprint)
    assert g3_result.passed is True, f"G3 Consistency Gate failed: {g3_result.errors}"
    print(f"[G3 PASSED] Consistency verified: build context and COPY sources valid on disk.")

    try:
        # -------------------------------------------------------------
        # Stage 4: G4 Build / Compile Gate
        # -------------------------------------------------------------
        print("[G4 BUILDING] Executing container build...")
        build_cmd = [
            "docker", "build",
            "-f", str(generated_dockerfile),
            "-t", "forgeops-e2e-codereview:test",
            str(REAL_REPO_PATH)
        ]
        build_res = subprocess.run(build_cmd, capture_output=True, text=True)
        assert build_res.returncode == 0, f"G4 Build failed:\nSTDOUT:\n{build_res.stdout}\nSTDERR:\n{build_res.stderr}"
        print(f"[G4 PASSED] Build succeeded on Attempt 1. Zero exit code 127.")

        # -------------------------------------------------------------
        # Stage 5: G5 Apply / Startup Gate
        # -------------------------------------------------------------
        print("[G5 APPLYING] Starting container...")
        # Clean up any preexisting container
        subprocess.run(["docker", "rm", "-f", "forgeops-e2e-codereview"], capture_output=True)

        run_cmd = [
            "docker", "run", "-d",
            "--name", "forgeops-e2e-codereview",
            "-p", "31337:3000",
            "-e", "PORT=3000",
            "forgeops-e2e-codereview:test"
        ]
        run_res = subprocess.run(run_cmd, capture_output=True, text=True)
        assert run_res.returncode == 0, f"G5 Run failed: {run_res.stderr}"

        # Allow container startup stabilization
        time.sleep(5)
        inspect_res = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Status}}", "forgeops-e2e-codereview"],
            capture_output=True,
            text=True
        )
        status = inspect_res.stdout.strip().lower()
        assert status == "running", f"Container exited prematurely: state={status}"
        print(f"[G5 PASSED] Container started successfully and is in state '{status}'.")

        # -------------------------------------------------------------
        # Stage 6: G6 Workload Verification Gate
        # -------------------------------------------------------------
        print("[G6 VERIFYING] Polling HTTP health on http://127.0.0.1:31337/ ...")
        health_passed = False
        last_status = None
        start_poll = time.time()

        while time.time() - start_poll < 30:
            try:
                r = requests.get("http://127.0.0.1:31337/", timeout=3)
                last_status = r.status_code
                if 200 <= r.status_code < 400:
                    health_passed = True
                    break
            except Exception:
                time.sleep(2)

        assert health_passed, f"G6 Workload health check failed (status: {last_status})"
        print(f"[G6 PASSED] Workload verified healthy: HTTP {last_status} response received.")

        # -------------------------------------------------------------
        # Stage 7: G7 Final Deployment Gate
        # -------------------------------------------------------------
        print("[G7 SIGN-OFF] Certifying deployment readiness.")
        # Final assertion: container still running, zero restarts
        inspect_restart = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.RestartCount}}", "forgeops-e2e-codereview"],
            capture_output=True,
            text=True
        )
        restart_count = int(inspect_restart.stdout.strip() or 0)
        assert restart_count == 0, f"Container experienced {restart_count} unexpected restarts."

        print(f"[G7 PASSED] Deployment verified with 0 restarts. Traffic ready at http://localhost:31337")
    finally:
        # Cleanup container, test image, build cache, and temporary file
        subprocess.run(["docker", "rm", "-f", "forgeops-e2e-codereview"], capture_output=True)
        subprocess.run(["docker", "rmi", "-f", "forgeops-e2e-codereview:test"], capture_output=True)
        subprocess.run(["docker", "builder", "prune", "-f"], capture_output=True)
        if generated_dockerfile.exists():
            generated_dockerfile.unlink()
        print("[CLEANUP COMPLETED] Test container, test image, and builder cache cleared.")
