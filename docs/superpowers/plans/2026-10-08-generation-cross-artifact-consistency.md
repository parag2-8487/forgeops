# Generation Cross-Artifact Consistency & Compulsory Compose Implementation Plan

- **Goal:** Enforce cross-artifact consistency across `Dockerfile`, `docker-compose.yml`, and `k8s/*` manifests, make `docker-compose.yml` compulsory, and eliminate repair drift across generation attempts.
- **Spec Reference:** `docs/superpowers/specs/2026-10-08-generation-cross-artifact-consistency-design.md`

---

## Task 1: Cross-Artifact Consistency Validator

**File:** `backend/src/generation/artifact_checks.py`
**Test File:** `backend/tests/unit/test_artifact_checks.py`

- Implement `_extract_container_port(artifact: Any) -> int | None`
- Implement `_extract_k8s_ports(doc: dict) -> tuple[int | None, int | None]` (containerPort, targetPort)
- Implement `_extract_compose_ports(doc: dict) -> list[int]`
- Implement `validate_cross_artifact_consistency(files: Sequence[Any]) -> list[str]`
- Integrate `validate_cross_artifact_consistency` into `validate_artifacts(files: Sequence[Any]) -> list[str]`
- Write tests in `test_artifact_checks.py` covering:
  - Dockerfile port (3000) vs k8s containerPort (8080) mismatch
  - Dockerfile port (3000) vs compose ports (8080) mismatch
  - Service targetPort vs Deployment containerPort mismatch
  - All matching ports (3000) passing without findings

---

## Task 2: Make `docker-compose.yml` a Required Artifact

**File:** `backend/src/generation/model_prompt.py`
**Test File:** `backend/tests/unit/test_generation_model_prompt.py`

- Update `REQUIRED_ARTIFACTS` in `model_prompt.py`:
  ```python
  REQUIRED_ARTIFACTS: tuple[str, ...] = (
      "Dockerfile",
      "docker-compose.yml",
      "k8s/deployment.yaml",
      "k8s/service.yaml",
      "k8s/ingress.yaml",
  )
  OPTIONAL_ARTIFACTS: tuple[str, ...] = ()
  ```
- Update `output_format_section` and `build_generation_prompt` text to reflect 5 required files.
- Update tests in `test_generation_model_prompt.py` to expect `docker-compose.yml` in `REQUIRED_ARTIFACTS`.

---

## Task 3: Repair Loop Invalidation & Port/Name Locking

**File:** `backend/src/generation/service.py`
**Test File:** `backend/tests/unit/test_generation_routing.py`

- In `service.py`:
  - When constructing repair prompts, inject strict immutable port and service name constraints.
  - In `carried` handling: if a cross-artifact mismatch finding occurs against a carried file, evict conflicting carried files so they are regenerated together.
  - In `_render` / fallback compose synthesis: extract port from accepted Dockerfile or Deployment manifest.

---

## Task 4: End-to-End Test & Verification

- Run `pytest backend/tests/unit/test_artifact_checks.py`
- Run `pytest backend/tests/unit/test_generation_model_prompt.py`
- Run `pytest backend/tests/unit/test_prompt_compiler.py`
- Run `pytest backend/tests/unit/test_generation_routing.py`
- Git commit all changes
