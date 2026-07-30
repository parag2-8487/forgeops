# SPDX-License-Identifier: FSL-1.1-ALv2
"""The compose validator's image-pinning rules (design.md §0.5 debt D5, §8.4, §13.3).

Four failure modes, each with its own fixture:

1. an image with **no digest** — behind an optional profile, because that is exactly
   how `infisical/infisical:v0.91.1` stayed unpinned while every other image carried
   a digest: the old rule looked at a fixed list of default services, so an optional
   profile was outside its view;
2. a surviving `<committed-digest>` **placeholder**, which looks pinned in a diff and
   fails only at `docker compose up`;
3. a digest with **no tag**, which is reproducible but leaves a reviewer unable to
   tell which version is deployed;
4. a `user:` override putting a container back on **uid 0** — the rule D-51 put in
   place of D5's `-rootless` tag-suffix check, whose counterexample is a correctly
   pinned non-root image reconfigured as root.

The real `docker-compose.yml` must pass all four.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.mandatory

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "check-compose-validate.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "compose"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("forgeops_check_compose_validate", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


VALIDATOR = _load()


def _errors(fixture: str) -> list[str]:
    return VALIDATOR.check(str(FIXTURES / fixture))


class TestEachPinningFailureIsDetected:
    def test_an_image_without_a_digest_is_rejected(self) -> None:
        errors = _errors("undigested-image.yml")
        assert any("digest-pinned" in e for e in errors), errors

    def test_the_rule_reaches_optional_profiles(self) -> None:
        """The clause debt D5 was actually about."""
        errors = _errors("undigested-image.yml")
        assert any("vaultish" in e for e in errors), (
            "the unpinned image behind an optional profile was not reported; the rule "
            "is still scoped to a fixed service list"
        )

    def test_a_surviving_digest_placeholder_is_rejected(self) -> None:
        errors = _errors("placeholder-digest.yml")
        assert any("placeholder" in e for e in errors), errors

    def test_a_placeholder_is_not_merely_reported_as_pinned(self) -> None:
        """`@sha256:<committed-digest>` contains `@sha256:`, so a naive substring
        test would call it pinned and say nothing."""
        errors = _errors("placeholder-digest.yml")
        assert errors, "a placeholder digest produced no findings at all"

    def test_a_digest_without_a_tag_is_rejected(self) -> None:
        errors = _errors("tagless-digest.yml")
        assert any("explicit tag" in e for e in errors), errors


class TestTheRealComposeFilePasses:
    def test_the_committed_compose_file_has_no_pinning_errors(self) -> None:
        errors = VALIDATOR.check(str(REPO_ROOT / "docker-compose.yml"))
        pinning = [e for e in errors if "digest" in e or "explicit tag" in e or "placeholder" in e]
        assert not pinning, pinning

    def test_every_image_reference_carries_a_digest(self) -> None:
        """Read straight from the file, so the assertion does not depend on the
        validator's own service lists being complete."""
        import yaml

        document = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        unpinned = [
            f"{name}: {svc['image']}"
            for name, svc in document["services"].items()
            if svc.get("image") and "@sha256:" not in svc["image"]
        ]
        assert not unpinned, unpinned


class TestTheOpaRootlessClauseIsRecorded:
    """Debt D5's second clause asked for `openpolicyagent/opa:1.4.2-rootless`.

    That tag does not exist — OPA 1.x publishes `1.4.2`, `-static`, `-debug`,
    `-envoy*` and `-istio*` and no `-rootless` variant; the suffix was retired after
    the 0.x line. The premise is also no longer true: the pinned image already runs
    as uid 1000 on a Chainguard apko base.

    D-51 replaces the retired suffix check with two things that mean something: a
    static rule that no service overrides `user:` back to uid 0 (below), and a
    runtime assertion in `compose-smoke` that reads `id -u` out of the running
    container. This test pins the two facts the decision rests on, so if OPA ever
    publishes a rootless variant, or the pinned reference changes, the reasoning is
    re-examined rather than silently inherited.
    """

    def test_the_opa_reference_is_the_pinned_1_4_2_digest(self) -> None:
        import yaml

        document = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        image = document["services"]["opa"]["image"]
        assert image.startswith("openpolicyagent/opa:1.4.2@sha256:"), image

    def test_the_reasoning_is_recorded_at_the_point_of_use(self) -> None:
        text = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        assert "-rootless" in text, "the D5 OPA finding is no longer documented in the compose file"
        assert "1000:1000" in text, "the non-root evidence is no longer recorded beside the image"


class TestNoServiceClimbsBackToRoot:
    """The rule that replaced the `-rootless` suffix check (D-51).

    The fixture is the counterexample the retired gate could not catch: the correct
    non-root image, correctly digest-pinned, handed back to uid 0 by `user:`.
    """

    def test_a_numeric_root_override_is_rejected(self) -> None:
        errors = _errors("root-user-override.yml")
        assert any("'opa'" in e and "uid 0" in e for e in errors), errors

    def test_the_named_root_spelling_is_also_rejected(self) -> None:
        errors = _errors("root-user-override.yml")
        assert any("'sidecar'" in e and "uid 0" in e for e in errors), errors

    def test_the_offending_file_would_have_passed_a_suffix_check(self) -> None:
        """States the reason the old gate was wrong, executably."""
        import yaml

        document = yaml.safe_load((FIXTURES / "root-user-override.yml").read_text(encoding="utf-8"))
        opa = document["services"]["opa"]
        assert "@sha256:" in opa["image"], "fixture must be correctly pinned to make its point"
        assert VALIDATOR._uid_is_root(opa["user"]), "fixture must actually request root"

    @pytest.mark.parametrize("value", ["0", "0:0", "root", "root:root", "0:root"])
    def test_every_spelling_compose_accepts_is_recognised(self, value: str) -> None:
        assert VALIDATOR._uid_is_root(value), value

    @pytest.mark.parametrize("value", ["1000", "1000:1000", "forgeops", "10:0extra", "root2"])
    def test_a_non_root_user_is_not_flagged(self, value: str) -> None:
        assert not VALIDATOR._uid_is_root(value), value

    def test_the_real_compose_file_overrides_no_user(self) -> None:
        import yaml

        document = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        rooted = [name for name, svc in document["services"].items() if VALIDATOR._uid_is_root(svc.get("user", "-"))]
        assert not rooted, rooted
