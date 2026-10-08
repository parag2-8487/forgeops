# Design Spec: Generation Cross-Artifact Consistency & Compulsory Docker Compose

- **Date:** 2026-10-08
- **Topic:** Cross-Artifact Consistency Gate, Compulsory `docker-compose.yml`, and Repair Drift Elimination
- **Target Subsystem:** `backend/src/generation/` (`artifact_checks.py`, `model_prompt.py`, `service.py`, `prompt_compiler.py`)

---

## 1. Context & Problem Statement

When deploying applications (such as the Portfolio project), deployment fails due to discrepancies generated across artifacts:
1. **Port Inconsistency**:
   - `Dockerfile` declares/binds port `3000`.
   - `docker-compose.yml` maps port `3000:3000`.
   - `k8s/deployment.yaml` specifies `containerPort: 8080`.
   - `k8s/service.yaml` targets `targetPort: 8080`.
   Individually, each manifest was syntactically valid YAML/Dockerfile syntax. Collectively, the deployment fails because the container listens on 3000 while Kubernetes routes to 8080.
2. **Naming & Identity Inconsistency**:
   - The project is named `portfolio`.
   - The model generates Kubernetes manifests naming `my-service` while Docker Compose names `portfolio`.
3. **Repair Drift in Iterations**:
   - In Attempt 1, the model emits all manifests on port `8080` with name `my-service`. The Dockerfile fails validation due to a missing `HEALTHCHECK`.
   - The repair loop carries forward `k8s/*` and narrows the prompt to repair `Dockerfile` only.
   - On Attempt 2 & 3, the model repairs `Dockerfile` but changes its port to `3000`.
   - The backend carries forward Attempt 1's `k8s/*` (port 8080) and merges with Attempt 3's `Dockerfile` (port 3000).
   - In the frontend SSE stream, all 3 attempts were appended, resulting in multiple conflicting Dockerfiles displayed in sequence.
4. **`docker-compose.yml` is Not Compulsory**:
   - `docker-compose.yml` is currently in `OPTIONAL_ARTIFACTS`.
   - When the model omits it, `service.py` synthesizes a template Compose file on a hardcoded port (3000) that can diverge from the model's Kubernetes manifests.

---

## 2. Goals & Success Criteria

1. **Compulsory Docker Compose**:
   - `docker-compose.yml` is a mandatory required artifact in generation (`REQUIRED_ARTIFACTS`).
   - The model is strictly instructed to generate it alongside `Dockerfile` and `k8s/*`.
2. **Deterministic Cross-Artifact Consistency Gate**:
   - [`validate_artifacts`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/backend/src/generation/artifact_checks.py#L377-L391) executes cross-artifact validation comparing `Dockerfile`, `docker-compose.yml`, and `k8s/*`.
   - Rejects the artifact set with explicit findings if:
     - Target container port differs between `Dockerfile`, `docker-compose.yml`, and `k8s/deployment.yaml` / `k8s/service.yaml`.
     - `k8s/service.yaml` selector labels do not match `k8s/deployment.yaml` pod labels.
     - `k8s/ingress.yaml` backend service name does not match `k8s/service.yaml` metadata name.
3. **Elimination of Repair Drift**:
   - Repair prompts lock the canonical port and service name as immutable constraints.
   - If a cross-artifact mismatch is detected after an attempt, conflicting carried companion artifacts are invalidated rather than merged blindly.
4. **Clean Streaming & Synthesis Alignment**:
   - Any fallback synthesis of `docker-compose.yml` extracts the port and image name from the existing `Dockerfile` / `k8s` manifest rather than assuming defaults.

---

## 3. Detailed Architecture & Implementation

### 3.1 Compulsory `docker-compose.yml`
- **Location:** [`backend/src/generation/model_prompt.py`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/backend/src/generation/model_prompt.py#L51-L64)
- **Changes:**
  - Move `"docker-compose.yml"` from `OPTIONAL_ARTIFACTS` to `REQUIRED_ARTIFACTS`:
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
  - Update `output_format_section` and `build_generation_prompt` to state 5 required files.
  - Update tests that assert `len(REQUIRED_ARTIFACTS) == 4` to 5.

### 3.2 Cross-Artifact Consistency Validator
- **Location:** [`backend/src/generation/artifact_checks.py`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/backend/src/generation/artifact_checks.py)
- **New Function:** `validate_cross_artifact_consistency(files: Sequence[GeneratedFile]) -> list[str]`
  1. **Port Extraction:**
     - `Dockerfile`: extracts exposed/listen port via `_port_disagreements` logic (`_EXPOSE`, `_CMD_PORT_PATTERNS`, `_ENV_PORT`).
     - `docker-compose.yml`: parses YAML, extracts container port from `services.<svc>.ports` (e.g. `3000:3000` -> container port `3000`) or `services.<svc>.expose`.
     - `k8s/deployment.yaml`: parses YAML, extracts `spec.template.spec.containers[*].ports[*].containerPort`.
     - `k8s/service.yaml`: parses YAML, extracts `spec.ports[*].targetPort` and `spec.ports[*].port`.
  2. **Port Parity Check:**
     - The container port in `Dockerfile`, `docker-compose.yml`, `k8s/deployment.yaml`, and `k8s/service.yaml`'s `targetPort` must all be identical integers.
     - If mismatch is detected, emit:
       `"cross-artifact: port mismatch across artifacts: {ports_summary}. All artifacts must target the same container port."`
  3. **Service & Selector Parity Check:**
     - In `k8s/deployment.yaml`: extract `spec.template.metadata.labels` and `spec.selector.matchLabels`.
     - In `k8s/service.yaml`: extract `spec.selector`.
     - Verify `service.spec.selector` subset matches `deployment.spec.template.metadata.labels`. If mismatch, emit:
       `"cross-artifact: k8s/service.yaml selector {svc_selector} does not match k8s/deployment.yaml labels {dep_labels}."`
     - In `k8s/ingress.yaml`: extract backend service name. Verify it equals `k8s/service.yaml` `metadata.name`.
     - If mismatch, emit:
       `"cross-artifact: k8s/ingress.yaml backend service '{ingress_svc}' does not match k8s/service.yaml name '{service_name}'."`
  4. **Integration:**
     - Called at the end of `validate_artifacts(files)`.

### 3.3 Repair Loop Hardening & Drift Elimination
- **Location:** [`backend/src/generation/service.py`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/backend/src/generation/service.py#L750-L800)
- **Changes:**
  - When narrowing repair prompts:
    - Include explicit port and application name enforcement:
      `"CRITICAL RULE: The authoritative port is {facts.port} and app name is '{facts.app_name}'. You MUST NOT change the port or service name."`
  - In `carried` handling:
    - When `gate_findings` contains a `"cross-artifact:"` finding involving a carried artifact:
      - If the newly generated artifact conflicts with a previously carried artifact, the carried artifact is evicted from `carried` and marked for repair rather than retained.

### 3.4 Fallback Compose Port Synchronization
- **Location:** [`backend/src/generation/service.py`](file:///C:/IMP/antigravity-cli/Major%20Project/Devops%20Automation/backend/src/generation/service.py#L958-L963)
- **Changes:**
  - If `docker-compose.yml` is ever synthesized via `_render`, parse the container port from the accepted `Dockerfile` or `k8s/deployment.yaml` rather than using a static default `3000`.

---

## 4. Verification Plan

1. **Unit Tests**:
   - `test_cross_artifact_port_consistency`: test matching ports pass; mismatch between Dockerfile (3000) and k8s (8080) fails.
   - `test_cross_artifact_service_selector_consistency`: test matching labels pass; mismatched selector fails.
   - `test_cross_artifact_ingress_backend_consistency`: test matching ingress backend service pass; mismatched name fails.
   - `test_docker_compose_is_required_artifact`: verify `parse_artifacts` requires `docker-compose.yml`.
2. **Regression Tests**:
   - Run `pytest backend/tests/unit/test_artifact_checks.py`.
   - Run `pytest backend/tests/unit/test_generation_model_prompt.py`.
   - Run `pytest backend/tests/unit/test_prompt_compiler.py`.
   - Run `pytest backend/tests/unit/test_generation_routing.py`.
3. **End-to-End Consistency**:
   - Verify all generated artifacts for any project share matching port and service names.
