# SPDX-License-Identifier: FSL-1.1-ALv2
"""An existing file is edited, not replaced — and that includes a dependency manifest.

Every generated artifact used to be a `create`, and `mutate/apply.go` refuses a create whose target
already exists. So a project with an incomplete `requirements.txt` could be handed a complete one and
could never apply it: the change set reached the agent, the agent refused the whole set with a conflict,
and it ended `rolled_back`.

Worse, if it HAD applied, a create carries no pre-image — so `change_items.old_hash` would have been the
hash of nothing and the §6.3 stale-apply guard would have had nothing to compare. An edit to a manifest
somebody hand-maintains is exactly the case where that guard matters.

These pin the decision rule rather than the diff rendering: an `update` carries `old_content`, which is
what makes the review a line-level diff and the apply refusable.
"""

from __future__ import annotations

import pytest
from src.generation.routes import _change_item_for

pytestmark = [pytest.mark.mandatory]

EXISTING_REQUIREMENTS = "fastapi==0.115.0\nuvicorn\n"
COMPLETED_REQUIREMENTS = "fastapi==0.115.0\nuvicorn\nrequests==2.32.3\n"


def test_an_existing_manifest_is_edited_rather_than_replaced() -> None:
    item = _change_item_for(
        "requirements.txt",
        COMPLETED_REQUIREMENTS,
        {"requirements.txt": EXISTING_REQUIREMENTS},
    )
    assert item.action == "update"
    # The pre-image is carried, which is what `change_items.old_hash` is computed from and what the
    # agent recomputes before writing. A create would leave the guard nothing to compare.
    assert item.old_content == EXISTING_REQUIREMENTS
    assert item.new_content == COMPLETED_REQUIREMENTS


def test_the_edit_preserves_what_the_developer_already_wrote() -> None:
    """The lines that were there survive into the post-image.

    The apply path is a faithful writer, not a merger: preservation is a property of the CONTENT the
    model produced, and this is where it is checkable before anything reaches a disk.
    """
    item = _change_item_for(
        "requirements.txt",
        COMPLETED_REQUIREMENTS,
        {"requirements.txt": EXISTING_REQUIREMENTS},
    )
    assert item.new_content is not None
    for line in EXISTING_REQUIREMENTS.splitlines():
        assert line in item.new_content, (
            f"the edit dropped {line!r}, which the developer wrote; a manifest patch must add the "
            "missing lines rather than replace the file"
        )


def test_a_manifest_that_does_not_exist_is_created() -> None:
    """A create is correct when there is nothing to preserve, and is the only honest claim available."""
    item = _change_item_for("requirements.txt", COMPLETED_REQUIREMENTS, {})
    assert item.action == "create"
    assert item.old_content is None


def test_a_path_with_no_trustworthy_preimage_is_created_not_updated() -> None:
    """A file whose stored text is not the bytes on disk yields no pre-image, so no update is claimed.

    `_editable_preimages` returns only files the scan redacted nothing from, because `file_contents`
    holds REDACTED text and hashing it for a redacted file would produce an `old_hash` the agent can
    never match. Absent from the mapping means "no honest pre-image", and the only thing that can be
    claimed then is a create — which the agent will refuse if the file exists, loudly and by name,
    rather than overwriting something the server never read.
    """
    item = _change_item_for("Dockerfile", "FROM scratch\n", {"requirements.txt": "x\n"})
    assert item.action == "create"


def test_windows_separators_do_not_defeat_the_lookup() -> None:
    """A backslash path must match the slash-separated key the index stores.

    Otherwise every edit on a Windows host silently becomes a create, and every apply refuses.
    """
    item = _change_item_for(
        "k8s\\deployment.yaml",
        "apiVersion: apps/v1\n",
        {"k8s/deployment.yaml": "apiVersion: v1\n"},
    )
    assert item.action == "update"
    assert item.old_content == "apiVersion: v1\n"
