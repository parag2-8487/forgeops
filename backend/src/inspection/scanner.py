"""Dynamic repository tree scanner discovering ecosystem manifests, lockfiles and artifacts.

Performs recursive filesystem inspection without hardcoding directory names
such as 'frontend', 'backend', 'client', or 'ui'.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

IGNORED_DIRECTORIES: set[str] = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".next",
    ".nuxt",
    ".cache",
    ".turbo",
    "dist",
    "build",
    "out",
    "target",
    "vendor",
    "__pycache__",
    ".pytest_cache",
    ".venv",
    "venv",
    "env",
    "bin",
    "obj",
    ".idea",
    ".vscode",
}

MANIFEST_FILENAMES: set[str] = {
    "package.json",
    "pnpm-workspace.yaml",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
    "Cargo.toml",
    "go.mod",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "Pipfile",
    "composer.json",
    "mix.exs",
    "Gemfile",
}

LOCKFILE_FILENAMES: set[str] = {
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lockb",
    "package-lock.json",
    "poetry.lock",
    "uv.lock",
    "Pipfile.lock",
    "Cargo.lock",
    "go.sum",
    "composer.lock",
    "Gemfile.lock",
}

RUNTIME_HINT_FILENAMES: set[str] = {
    ".nvmrc",
    ".node-version",
    ".python-version",
    ".ruby-version",
    "runtime.txt",
}

EXISTING_CONTAINER_FILENAMES: set[str] = {
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
}


@dataclass
class DiscoveredFile:
    """Metadata regarding a discovered file in the repository tree."""

    relative_path: str
    absolute_path: str
    filename: str
    directory: str
    depth: int


@dataclass
class DiscoveredRepository:
    """Aggregated manifest, lockfile, and container discoveries across a repository."""

    root_path: str
    manifests: list[DiscoveredFile] = field(default_factory=list)
    lockfiles: list[DiscoveredFile] = field(default_factory=list)
    runtime_hints: list[DiscoveredFile] = field(default_factory=list)
    existing_artifacts: list[DiscoveredFile] = field(default_factory=list)
    directory_tree_snippet: str = ""

    def get_manifests_by_type(self, filename: str) -> list[DiscoveredFile]:
        return [m for m in self.manifests if m.filename == filename]

    def has_manifest(self, filename: str) -> bool:
        return any(m.filename == filename for m in self.manifests)


def scan_repository_tree(root_path: str | Path, max_depth: int = 6) -> DiscoveredRepository:
    """Recursively scans a repository root up to max_depth, ignoring cache/vendor dirs.

    Zero assumptions are made regarding directory names.
    """
    root = Path(root_path).resolve()
    discovered = DiscoveredRepository(root_path=str(root))
    tree_lines: list[str] = []

    for dirpath, dirnames, filenames in os.walk(root):
        # Prune ignored directories in-place to avoid descending into them
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRECTORIES]

        current_path = Path(dirpath)
        try:
            rel_dir = current_path.relative_to(root)
            depth = len(rel_dir.parts)
        except ValueError:
            depth = 0

        if depth > max_depth:
            dirnames.clear()
            continue

        indent = "  " * depth
        if depth > 0:
            tree_lines.append(f"{indent}{current_path.name}/")

        for f in filenames:
            rel_file = (current_path / f).relative_to(root).as_posix()
            file_meta = DiscoveredFile(
                relative_path=rel_file,
                absolute_path=str(current_path / f),
                filename=f,
                directory=rel_dir.as_posix() if depth > 0 else ".",
                depth=depth,
            )

            # Check for manifests (exact name or *.csproj / *.sln)
            if f in MANIFEST_FILENAMES or f.endswith(".csproj") or f.endswith(".sln") or f.endswith(".fsproj"):
                discovered.manifests.append(file_meta)
                tree_lines.append(f"{indent}  * {f} [manifest]")
            elif f in LOCKFILE_FILENAMES:
                discovered.lockfiles.append(file_meta)
                tree_lines.append(f"{indent}  * {f} [lockfile]")
            elif f in RUNTIME_HINT_FILENAMES:
                discovered.runtime_hints.append(file_meta)
                tree_lines.append(f"{indent}  * {f} [runtime-hint]")
            elif f in EXISTING_CONTAINER_FILENAMES:
                discovered.existing_artifacts.append(file_meta)
                tree_lines.append(f"{indent}  * {f} [container-artifact]")

    discovered.directory_tree_snippet = "\n".join(tree_lines[:150])
    return discovered
