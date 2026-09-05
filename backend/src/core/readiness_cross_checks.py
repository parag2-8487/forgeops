# SPDX-License-Identifier: FSL-1.1-ALv2
"""Checks that judge the six categories against each other rather than one at a time.

WHY A CATEGORY-AT-A-TIME SCORE MISSES THE WORST FAULTS

Every check above these scores one artifact in isolation, so a repository can hold six internally
correct categories that describe six different pieces of software and score full marks:

  * a workflow that runs `docker build` in a repository with no Dockerfile — the pipeline is valid
    YAML with real steps, and it cannot succeed
  * a Deployment pulling `registry/app` that no workflow ever pushes — the manifest is well formed,
    pins its tag, sets its limits, and points at an image nothing produces
  * a `.env.example` that omits a variable the compose file passes to the container — the example
    exists, so `env_example_present` is satisfied, and following it still produces a service that
    cannot start

Each of those is invisible to a check that looks at one file. They are also the failures most likely
to reach production, because every individual artifact reviews cleanly.

WHAT THESE DELIBERATELY DO NOT DO

They never report an inconsistency between two things when one of them is absent. A repository with no
Kubernetes manifests has no image mismatch, and saying so would be a second failure for the same fact —
`kubernetes_manifests_present` already reports it, and a reader would be told the manifests were wrong
rather than missing. Every check here returns "nothing to compare" in that case, which is a third
outcome distinct from pass and fail.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Final

from pydantic import BaseModel

from .manifest_facts import _containers, _documents, _jobs, _pod_specs, _steps, _workflow_documents, _yaml_bodies

#: Commands that build a container image, as they appear in a `run:` step.
_BUILD_COMMAND: Final = re.compile(r"\bdocker(?:\s+buildx)?\s+build\b|\bpodman\s+build\b|\bnerdctl\s+build\b")

#: The `-f`/`--file` argument of a build command, which names a Dockerfile other than the default.
_BUILD_FILE: Final = re.compile(r"(?:-f|--file)[=\s]+(\S+)")

#: Actions that build and push an image without a shell command.
_BUILD_ACTIONS: Final = ("docker/build-push-action", "redhat-actions/buildah-build")


class CrossCheck(BaseModel):
    """The outcome of comparing two categories.

    `comparable` is the third outcome. `passed` alone cannot distinguish "the two agree" from "there was
    nothing to compare", and scoring the second as a failure would report a missing artifact twice — once
    honestly, and once as a consistency fault it is not.
    """

    comparable: bool
    passed: bool
    #: What was compared and what disagreed, naming both sides.
    detail: str
    #: The path a reader should open.
    evidence: str = ""
    #: The line the disagreement sits on, when it is in a file with lines.
    line: int | None = None

    model_config = {"frozen": True}


def _line_of(body: str, needle: str) -> int | None:
    if not needle:
        return None
    for number, text in enumerate(body.splitlines(), start=1):
        if needle in text:
            return number
    return None


def _body_for(path: str, contents: Mapping[str, str]) -> str:
    return contents.get(path.lower(), "") or contents.get(path, "")


def ci_builds_an_image_that_has_a_dockerfile(
    paths: Iterable[str], contents: Mapping[str, str], dockerfile: str
) -> CrossCheck:
    """A workflow that builds an image must have a Dockerfile to build.

    Catches the case where the containerization category scores zero for having no Dockerfile and the
    CI category scores full marks for a pipeline whose central step cannot run. Both numbers are
    correct on their own and the pair is nonsense.

    When a build names an explicit `-f <path>`, that path is what must exist — a repository can hold a
    `Dockerfile` at the root and a workflow that builds `docker/api.Dockerfile`, and the check that only
    asks "is there a Dockerfile" would pass it.
    """
    indexed = {p.replace("\\", "/").lower() for p in paths}
    for path, document in _workflow_documents(paths, contents):
        body = _body_for(path, contents)
        for _job_name, job in _jobs(document):
            for step in _steps(job):
                run = step.get("run")
                uses = step.get("uses")

                named_file = ""
                builds = False
                if isinstance(run, str) and _BUILD_COMMAND.search(run):
                    builds = True
                    match = _BUILD_FILE.search(run)
                    if match:
                        named_file = match.group(1).strip("\"'")
                elif isinstance(uses, str) and any(a in uses for a in _BUILD_ACTIONS):
                    builds = True
                    with_block = step.get("with")
                    if isinstance(with_block, Mapping):
                        candidate = with_block.get("file") or with_block.get("dockerfile")
                        if isinstance(candidate, str):
                            named_file = candidate.strip()

                if not builds:
                    continue

                if named_file:
                    wanted = named_file.replace("\\", "/").lstrip("./").lower()
                    if wanted not in indexed:
                        return CrossCheck(
                            comparable=True,
                            passed=False,
                            detail=(f"{path} builds {named_file!r}, and no such file is indexed in this repository"),
                            evidence=path,
                            line=_line_of(body, named_file),
                        )
                elif not dockerfile:
                    return CrossCheck(
                        comparable=True,
                        passed=False,
                        detail=(
                            f"{path} runs a container build, and this repository contains no Dockerfile for it to build"
                        ),
                        evidence=path,
                        line=_line_of(body, "build"),
                    )
                return CrossCheck(
                    comparable=True,
                    passed=True,
                    detail=f"{path} builds {named_file or dockerfile}, which is indexed",
                    evidence=path,
                )

    return CrossCheck(
        comparable=False,
        passed=False,
        detail="no workflow step builds a container image, so there is nothing to reconcile",
    )


def _image_repositories(paths: Iterable[str], contents: Mapping[str, str]) -> list[tuple[str, str, str]]:
    """`(path, container name, image repository)` for every container in every manifest.

    The repository is the image without its tag or digest, because that is the part a pipeline pushes
    to — comparing whole references would report a mismatch on every tag bump.
    """
    found: list[tuple[str, str, str]] = []
    for path, body in _yaml_bodies(paths, contents):
        for document in _documents(body):
            for pod_spec in _pod_specs(document):
                for index, container in enumerate(_containers(pod_spec)):
                    image = container.get("image")
                    if not isinstance(image, str) or not image:
                        continue
                    repository = image.split("@", 1)[0]
                    if ":" in repository.rsplit("/", 1)[-1]:
                        repository = repository.rsplit(":", 1)[0]
                    name = container.get("name")
                    found.append((path, str(name) if isinstance(name, str) else f"container[{index}]", repository))
    return found


def kubernetes_images_are_produced_by_ci(paths: Iterable[str], contents: Mapping[str, str]) -> CrossCheck:
    """A manifest must not deploy an image no pipeline in this repository builds or pushes.

    A manifest can pin its tag, bound its resources, declare both probes and still point at software
    nothing here produces. That manifest reviews cleanly and deploys whatever happens to be in the
    registry under that name, which may be another team's image or nothing at all.

    THE COMPARISON IS ON THE REPOSITORY, NOT THE FULL REFERENCE, and it is a substring match against
    the whole workflow text rather than a parse. A pipeline may assemble the image name from variables,
    so requiring a structural match would report a mismatch for every repository that does it properly.
    A substring match can be fooled by a coincidence; it cannot produce a false alarm on a correct
    repository, which is the direction that matters for a score people trust.
    """
    images = _image_repositories(paths, contents)
    if not images:
        return CrossCheck(
            comparable=False,
            passed=False,
            detail="no manifest names a container image, so there is nothing to reconcile",
        )

    workflow_text = ""
    workflow_paths: list[str] = []
    for path, _document in _workflow_documents(paths, contents):
        workflow_paths.append(path)
        workflow_text += "\n" + _body_for(path, contents)
    if not workflow_paths:
        return CrossCheck(
            comparable=False,
            passed=False,
            detail="there is no workflow to compare the manifests against",
        )

    for path, container, repository in images:
        # The last segment is what a pipeline most often names, because the registry and the owner are
        # usually supplied as variables or secrets.
        leaf = repository.rsplit("/", 1)[-1]
        if repository in workflow_text or (leaf and leaf in workflow_text):
            continue
        body = _body_for(path, contents)
        return CrossCheck(
            comparable=True,
            passed=False,
            detail=(
                f"container {container!r} in {path} deploys {repository!r}, and no workflow in this "
                f"repository mentions that image — nothing here builds or pushes it"
            ),
            evidence=path,
            line=_line_of(body, repository),
        )

    return CrossCheck(
        comparable=True,
        passed=True,
        detail=f"every image in {len(images)} container spec(s) is named by a workflow in this repository",
    )


def _declared_env_names(paths: Iterable[str], contents: Mapping[str, str]) -> dict[str, str]:
    """Variable names the deployment passes to a container, mapped to the file that passes them.

    Read from compose `environment:` and Kubernetes `env:`, which are the two places a running
    container's variables are declared in a repository. Values are never collected — only names.
    """
    declared: dict[str, str] = {}

    for path, body in _yaml_bodies(paths, contents):
        for document in _documents(body):
            services = document.get("services")
            if isinstance(services, Mapping):
                for service in services.values():
                    if not isinstance(service, Mapping):
                        continue
                    env = service.get("environment")
                    if isinstance(env, Mapping):
                        for name in env:
                            declared.setdefault(str(name), path)
                    elif isinstance(env, list):
                        for entry in env:
                            if isinstance(entry, str) and entry:
                                declared.setdefault(entry.split("=", 1)[0].split(":", 1)[0].strip(), path)
            for pod_spec in _pod_specs(document):
                for container in _containers(pod_spec):
                    env = container.get("env")
                    if isinstance(env, list):
                        for entry in env:
                            if isinstance(entry, Mapping):
                                name = entry.get("name")
                                if isinstance(name, str) and name:
                                    declared.setdefault(name, path)
    return declared


def _example_env_names(body: str) -> set[str]:
    names: set[str] = set()
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        names.add(stripped.split("=", 1)[0].strip())
    return names


def env_example_matches_the_deployment(
    example_path: str, example_body: str, paths: Iterable[str], contents: Mapping[str, str]
) -> CrossCheck:
    """Every variable the deployment passes must appear in the committed example, and vice versa.

    `env_example_present` asks whether the file exists. A file that exists and omits half the variables
    the compose file supplies is worse than no file, because it reads as a complete list: a contributor
    follows it exactly and gets a service that cannot start, with nothing to tell them what is missing.

    THE COMPARISON IS ON NAMES ONLY. §7.11 keeps values out of anything persisted, and a value in a
    committed example is a placeholder by definition, so comparing them would compare two things that
    are both deliberately not the truth.

    A variable in the example that the deployment does not pass is reported too, and separately: it is
    usually a variable the code reads with a default, and sometimes a leftover from a removed feature.
    Both are worth knowing and they are different from an omission.
    """
    if not example_path or not example_body:
        return CrossCheck(
            comparable=False,
            passed=False,
            detail="there is no committed example environment file to compare against",
        )
    declared = _declared_env_names(paths, contents)
    if not declared:
        return CrossCheck(
            comparable=False,
            passed=False,
            detail=(
                "no compose service or container spec declares environment variables, so there is "
                "nothing to reconcile the example against"
            ),
        )

    documented = _example_env_names(example_body)
    missing = sorted(name for name in declared if name not in documented)
    if missing:
        first = missing[0]
        source = declared[first]
        return CrossCheck(
            comparable=True,
            passed=False,
            detail=(
                f"{source} passes {len(missing)} variable(s) the example does not name "
                f"({', '.join(missing[:5])}{'…' if len(missing) > 5 else ''}); a contributor following "
                f"{example_path} would start the service without them"
            ),
            evidence=example_path,
            line=_line_of(_body_for(source, contents), first),
        )

    unused = sorted(name for name in documented if name not in declared)
    if unused:
        return CrossCheck(
            comparable=True,
            passed=False,
            detail=(
                f"{example_path} names {len(unused)} variable(s) no compose service or container spec "
                f"passes ({', '.join(unused[:5])}{'…' if len(unused) > 5 else ''}); either the "
                "deployment is missing them or they are left over from something removed"
            ),
            evidence=example_path,
            line=_line_of(example_body, unused[0]),
        )

    return CrossCheck(
        comparable=True,
        passed=True,
        detail=f"{example_path} names exactly the {len(declared)} variable(s) the deployment passes",
    )
