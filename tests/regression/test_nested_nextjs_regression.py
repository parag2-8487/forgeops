"""Explicit regression test for the nested Next.js subdirectory failure ('code-review/')."""

import json
import tempfile
from pathlib import Path
import pytest

from backend.src.generation.prompt_compiler import compile_dockerfile_prompt
from backend.src.inspection.blueprint_gate import verify_blueprint_gate
from backend.src.inspection.scanner import scan_repository_tree
from backend.src.inspection.workspace_resolver import resolve_repository_blueprint
from backend.src.validation.consistency_gate import verify_consistency_gate


def test_nested_nextjs_end_to_end_resolution():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        sub = root / "code-review"
        sub.mkdir()

        # Create nested Next.js project
        pkg_json = sub / "package.json"
        pkg_json.write_text(
            json.dumps({
                "name": "code-review-app",
                "version": "0.1.0",
                "scripts": {
                    "dev": "next dev",
                    "build": "next build",
                    "start": "next start",
                },
                "dependencies": {
                    "next": "14.2.0",
                    "react": "18.2.0",
                    "react-dom": "18.2.0",
                },
            })
        )

        # 1. Scanner finds nested manifest without path assumptions
        discovered = scan_repository_tree(root)
        assert any(m.filename == "package.json" and m.directory == "code-review" for m in discovered.manifests)

        # 2. Workspace resolver identifies code-review as application root
        bp = resolve_repository_blueprint(discovered)
        assert bp.build_config.source_dir == "code-review"
        assert bp.runtime.framework == "nextjs"
        assert bp.runtime.start_command == "npm run start"
        assert bp.network.listen_port == 3000

        # 3. G1 Blueprint Gate verifies blueprint
        gate1 = verify_blueprint_gate(bp)
        assert gate1.passed is True
        assert gate1.status == "PASSED"

        # 4. Prompt compiler generates prompt that respects code-review subdirectory
        prompt = compile_dockerfile_prompt(bp)
        assert "subfolder: 'code-review'" in prompt
        assert "Do NOT flatten the repository directory structure" in prompt
        assert "npm run build" in prompt

        # 5. Simulate valid generated Dockerfile respecting code-review
        df = root / "Dockerfile"
        df.write_text("""
FROM node:20-alpine AS builder
WORKDIR /app
COPY code-review/package*.json ./code-review/
RUN cd code-review && npm install
COPY code-review ./code-review
RUN cd code-review && npm run build

FROM node:20-alpine AS runner
WORKDIR /app/code-review
EXPOSE 3000
CMD ["npm", "start"]
""")

        # 6. G3 Pre-Execution Consistency Gate verifies Dockerfile against disk
        gate3 = verify_consistency_gate(root, df, None, bp)
        assert gate3.passed is True
        assert gate3.errors == []
