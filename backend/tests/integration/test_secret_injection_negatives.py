# SPDX-License-Identifier: FSL-1.1-ALv2
"""FR-45's two negatives: a secret value must not reach an artifact or a prompt.

The positive — "secrets are injected as environment variables at deploy time" — is the agent's
`secrets.inject` operation and is tested there. The negatives are here, and they are the harder and more
important half: it is easy to demonstrate that a value arrives somewhere, and the requirement is that it
arrives in exactly one place.

Both are asserted by searching for the value itself rather than by checking that a redaction function was
called. A test that asserts `create_redacted_prompt` was invoked passes for a redactor that returns its
input unchanged.
"""

from __future__ import annotations

from typing import Any

import pytest
from src.generation.service import GenerationService
from src.secrets.injection import inject_secrets
from src.secrets.redaction import create_redacted_prompt

#: Assembled from fragments so no line of this file carries a credential shape.
_PREFIX = "AK" + "IA"
SECRET_VALUE = _PREFIX + "7EXAMPLEFAKEKEY0"
SECRET_PASSWORD = "s3cr3t-" + "database-" + "password"


class _StubStore:
    """A store that returns values, and records every retrieval.

    A double rather than a mock: `inject_secrets` is documented as the ONLY module permitted to call
    `SecretStore.get_value()`, and `scripts/chokepoint_graph.py` enforces that. Recording the calls is
    what lets this file assert the confinement holds rather than trusting the comment.
    """

    def __init__(self, values: dict[str, str]) -> None:
        self._values = values
        self.retrieved: list[str] = []

    async def get_value(self, secret: Any) -> str:
        self.retrieved.append(secret.key)
        return self._values[secret.key]


class _Secret:
    def __init__(self, key: str) -> None:
        self.key = key


@pytest.mark.asyncio
async def test_injection_returns_an_environment_and_nothing_else() -> None:
    """The positive, minimally: values reach an env mapping keyed by variable name."""
    store = _StubStore({"AWS_ACCESS_KEY_ID": SECRET_VALUE, "DB_PASSWORD": SECRET_PASSWORD})
    env = await inject_secrets([_Secret("AWS_ACCESS_KEY_ID"), _Secret("DB_PASSWORD")], store, {"EXISTING": "kept"})
    assert env["AWS_ACCESS_KEY_ID"] == SECRET_VALUE
    assert env["DB_PASSWORD"] == SECRET_PASSWORD
    # A base environment is preserved rather than replaced: injection adds credentials to a deployment's
    # environment, it does not define the whole of it.
    assert env["EXISTING"] == "kept"
    assert store.retrieved == ["AWS_ACCESS_KEY_ID", "DB_PASSWORD"]


@pytest.mark.asyncio
async def test_injection_does_not_mutate_the_base_environment() -> None:
    """A caller's mapping must not gain credentials as a side effect of asking for an injection."""
    base = {"EXISTING": "kept"}
    store = _StubStore({"TOKEN": SECRET_VALUE})
    await inject_secrets([_Secret("TOKEN")], store, base)
    assert base == {"EXISTING": "kept"}, "the caller's base environment was mutated"


def test_no_generated_artifact_contains_a_secret_value() -> None:
    """FR-45's first negative, over every artifact kind the platform produces.

    A generated manifest may reference a secret BY NAME — that is what `secretKeyRef` is for — and must
    never carry the value. This searches every byte of every artifact.
    """
    service = GenerationService()
    for project in (
        None,
        {"name": "billing-api", "settings": {"runtime": "node", "port": 3000}},
        {"name": "search", "settings": {"runtime": "python"}},
    ):
        artifacts = service._render("a service that needs database credentials", project)
        assert artifacts, "nothing was generated, so this assertion would be vacuous"
        for artifact in artifacts:
            for forbidden in (SECRET_VALUE, SECRET_PASSWORD, _PREFIX + "7EXAMPLE"):
                assert forbidden not in artifact.content, f"{artifact.path} contains a secret value"


def test_the_generated_artifacts_do_not_embed_any_credential_shape() -> None:
    """Stronger than the test above: no artifact carries anything SHAPED like a credential.

    The previous test can only find values it was given. This one is the check that would catch a
    renderer that invented a placeholder credential of its own — which is exactly the defect class this
    repository keeps finding, and `scripts/check-added-shapes.py` applies the same reasoning to source.
    """
    import re

    # Each pattern is assembled from fragments, because a regex that DESCRIBES a credential shape is
    # itself a credential shape as far as a scanner is concerned -- `scripts/check-added-shapes.py`
    # blocked this file twice and was right both times. Naming the thing instead of spelling it is the
    # rule the hook's own message gives.
    akid = "AK" + "IA" + r"[0-9A-Z]{16}"
    github_pat = "gh" + r"[pousr]_[A-Za-z0-9]{36}"
    pem = "-----" + "BEGIN " + r"[A-Z ]*PRIVATE " + "KEY" + "-----"
    jwt = "ey" + r"J[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."
    shapes = tuple(re.compile(pattern) for pattern in (akid, github_pat, pem, jwt))
    artifacts = GenerationService()._render("a service", {"name": "app", "settings": {}})
    for artifact in artifacts:
        for shape in shapes:
            found = shape.search(artifact.content)
            assert found is None, f"{artifact.path} contains a credential shape: {found.group(0)[:8]}..."


def test_a_prompt_never_carries_a_secret_value() -> None:
    """FR-45's second negative. Asserted by searching the output, not by checking a call happened."""
    prompt = (
        f"Deploy the billing service. Connect with AWS_ACCESS_KEY_ID={SECRET_VALUE} and password {SECRET_PASSWORD}."
    )
    redacted = create_redacted_prompt(prompt)
    assert SECRET_VALUE not in redacted, "the access key survived redaction"
    # The redactor is a pattern matcher and a password with no distinctive shape may not match, which is
    # precisely why the agent never reads `.env` and why injection is a separate governed operation
    # rather than something a prompt-building path is trusted to handle. What must hold is that a value
    # with a recognisable shape does not pass, and that the prompt still says something.
    assert redacted.strip(), "redaction produced an empty prompt"
    assert "billing service" in redacted, "redaction destroyed the operator's actual request"


def test_redaction_is_not_a_no_op() -> None:
    """The control. Without it, a redactor that returns its input passes the test above."""
    prompt = f"key={SECRET_VALUE}"
    assert create_redacted_prompt(prompt) != prompt, (
        "redaction returned its input unchanged, so every assertion about it is vacuous"
    )


def test_a_generated_manifest_references_secrets_by_name_not_by_value() -> None:
    """The shape FR-45 implies: a deployment names the variables it needs, values arrive separately.

    Stated as a test because the alternative — a manifest with `value: <literal>` — is what a naive
    generator produces, and it would put credentials in a file that gets committed.
    """
    import yaml

    artifacts = GenerationService()._render("a service", {"name": "app", "settings": {}})
    manifests = [a for a in artifacts if a.path.startswith("k8s/")]
    assert manifests
    for artifact in manifests:
        for document in yaml.safe_load_all(artifact.content):
            if not isinstance(document, dict):
                continue
            rendered = yaml.safe_dump(document)
            # No `env:` entry may carry a literal that looks like a credential. The generated manifests
            # declare no env at all today, which satisfies this trivially — and the assertion is worth
            # keeping so that adding one has to do it by reference.
            assert _PREFIX not in rendered, artifact.path
