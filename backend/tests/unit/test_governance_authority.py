# SPDX-License-Identifier: FSL-1.1-ALv2
"""§2.2.1 mechanisms 1 and 2: the mint-only capability and the banned-api rule.

design.md §2.2.1, §5.4, §11.6; Q-03; tasks.md leaf 7.1.

Design intent is not enforcement, so each mechanism is exercised rather than described:

* **The type cannot be forged.** Construction with anything but the module-private sentinel
  raises `TypeError` — including with values that look plausible, because "plausible" is what
  an attacker or a well-meaning refactor reaches for first.
* **The sentinel cannot be named.** Ruff's banned-api rule is run for real, over a temporary
  file outside the package, and its exit status and diagnostic code are asserted. A table
  entry nobody has watched fail is a table entry that might be misspelled — and a misspelled
  entry bans nothing while looking exactly like one that does.
* **The marker makes primitives findable.** That is its only job, and the test says so, so
  nobody later mistakes it for validation it deliberately does not perform.
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from src.governance.authority import FORGERY_MESSAGE, MutationAuthority, mint_authority
from src.governance.primitives import (
    DECORATOR_NAME,
    MARKER_ATTRIBUTE,
    is_mutation_primitive,
    mutation_primitive,
)

pytestmark = pytest.mark.mandatory

BACKEND_ROOT = Path(__file__).resolve().parents[2]

#: Every symbol §2.2.1 confines to `governance/`, with the module it lives in. Driven through
#: the real linter below, one case each, so a missing or misspelled table entry is one red
#: test naming the symbol rather than a silent gap.
BANNED_IMPORTS: tuple[tuple[str, str], ...] = (
    ("src.governance.authority", "_MINT_SENTINEL"),
    ("src.governance.envelope", "sign_envelope"),
    ("src.governance.envelope", "_SIGNING_KEY"),
    # D-60's sixth entry. `_SIGNING_KEY` is a ContextVar and this is the only setter, so leaving
    # it unbanned would let an outsider install a key that a governance path missing its own
    # scope would then sign with — the ban is what keeps "no scope" and "raises" the same thing.
    ("src.governance.envelope", "signing_key_scope"),
    ("src.websocket.hub", "send_command"),
    ("src.auth.devices", "envelope_key"),
)


def _valid_kwargs() -> dict[str, object]:
    return {
        "change_set_id": uuid.uuid4(),
        "approval_id": uuid.uuid4(),
        "policy_bundle_digest": "sha256:" + "0" * 64,
        "blast_radius": "workspace",
        "audit_seq": 1,
        "envelope_digest": "sha256:" + "1" * 64,
    }


class TestTheAuthorityCanOnlyBeMinted:
    def test_mint_authority_produces_one(self) -> None:
        authority = mint_authority(**_valid_kwargs())  # type: ignore[arg-type]
        assert isinstance(authority, MutationAuthority)
        assert authority.audit_seq == 1

    @pytest.mark.parametrize(
        "sentinel",
        [
            None,
            object(),
            "MINT",
            0,
            True,
            (),
            MutationAuthority,
        ],
        ids=["none", "another-object", "a-string", "zero", "true", "empty-tuple", "the-class-itself"],
    )
    def test_construction_with_any_other_sentinel_raises(self, sentinel: object) -> None:
        """Parametrised over values a refactor would plausibly reach for. `object()` is the
        important one: it proves the check is an IDENTITY comparison, not a type check."""
        with pytest.raises(TypeError, match="may only be minted"):
            MutationAuthority(_sentinel=sentinel, **_valid_kwargs())  # type: ignore[arg-type]

    def test_the_sentinel_argument_is_required(self) -> None:
        """Omitting it must not default to something permissive."""
        with pytest.raises(TypeError):
            MutationAuthority(**_valid_kwargs())  # type: ignore[call-arg]

    def test_the_failure_message_points_at_the_design_and_the_property(self) -> None:
        """A `TypeError` saying only "wrong sentinel" would leave the next reader guessing why
        the type exists at all."""
        assert "§2.2.1" in FORGERY_MESSAGE
        assert "Q-03" in FORGERY_MESSAGE
        assert "governance.chokepoint" in FORGERY_MESSAGE

    def test_it_is_frozen(self) -> None:
        """An authority whose blast radius a handler could widen after the fact would make
        every downstream check advisory."""
        authority = mint_authority(**_valid_kwargs())  # type: ignore[arg-type]
        with pytest.raises((AttributeError, TypeError)):
            authority.blast_radius = "infrastructure"  # type: ignore[misc]

    def test_it_is_slotted_so_no_attribute_can_be_added(self) -> None:
        """An attribute a caller could attach is a place to smuggle state past the stages that
        produced the authority."""
        authority = mint_authority(**_valid_kwargs())  # type: ignore[arg-type]
        with pytest.raises((AttributeError, TypeError)):
            authority.extra = "smuggled"  # type: ignore[attr-defined]

    def test_the_sentinel_is_not_exported(self) -> None:
        """`__all__` omitting it is the documentation half; the banned-api rule below is the
        enforcement half. Both, because `__all__` only governs `import *`."""
        import src.governance.authority as module

        assert "_MINT_SENTINEL" not in module.__all__

    @pytest.mark.parametrize(
        ("field", "value"),
        [("audit_seq", 0), ("audit_seq", -1), ("policy_bundle_digest", "")],
        ids=["audit-seq-zero", "audit-seq-negative", "no-policy-digest"],
    )
    def test_mint_refuses_an_authority_that_cannot_have_been_earned(self, field: str, value: object) -> None:
        """`audit_seq` comes from a written audit record and `policy_bundle_digest` from the
        bundle that allowed the mutation. An authority missing either did not traverse the
        stages that produce them, so it is refused at mint time rather than detected later."""
        kwargs = _valid_kwargs()
        kwargs[field] = value
        with pytest.raises(ValueError, match="required|positive"):
            mint_authority(**kwargs)  # type: ignore[arg-type]

    def test_mint_is_keyword_only(self) -> None:
        """Every field is an opaque identifier, so a transposed positional pair would mint an
        authority naming the wrong change set and nothing downstream would notice."""
        import inspect

        parameters = inspect.signature(mint_authority).parameters
        assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in parameters.values()), parameters


class TestTheBannedApiRuleActuallyRejectsEachSymbol:
    """Ruff is run for real. Asserting the table's contents instead would assert that the
    table says what it says — the class of test this phase exists to eliminate."""

    @staticmethod
    def _run_ruff(source: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
        # Written inside the package so ruff resolves `backend/pyproject.toml`, but under a
        # name no per-file-ignore exempts — `src/governance/authority.py` and
        # `primitives.py` are exempt by design, so a file placed there would pass and prove
        # the opposite of what is intended.
        target = BACKEND_ROOT / "src" / "core" / f"_banned_api_probe_{tmp_path.name}.py"
        target.write_text(source, encoding="utf-8")
        try:
            return subprocess.run(  # noqa: S603 - fixed argv, no shell
                [sys.executable, "-m", "ruff", "check", "--no-cache", "--output-format", "json", str(target)],
                cwd=str(BACKEND_ROOT),
                capture_output=True,
                text=True,
                check=False,
            )
        finally:
            target.unlink(missing_ok=True)

    @pytest.mark.parametrize(("module", "symbol"), BANNED_IMPORTS, ids=[s for _m, s in BANNED_IMPORTS])
    def test_importing_it_is_a_tid251_violation(self, module: str, symbol: str, tmp_path: Path) -> None:
        completed = self._run_ruff(f"from {module} import {symbol}\n", tmp_path)
        assert completed.returncode != 0, f"ruff accepted `from {module} import {symbol}`: {completed.stdout[:400]}"
        codes = {finding.get("code") for finding in json.loads(completed.stdout or "[]")}
        assert "TID251" in codes, (
            f"`from {module} import {symbol}` was rejected, but not by the banned-api rule "
            f"(codes: {sorted(c for c in codes if c)}). The table entry is probably misspelled, "
            "which bans nothing while looking exactly like an entry that does."
        )

    def test_a_benign_import_from_the_same_modules_is_accepted(self, tmp_path: Path) -> None:
        """The control. If ruff rejected everything — a syntax error in the probe, a
        misconfigured cwd — every case above would pass for the wrong reason."""
        completed = self._run_ruff("from src.governance.authority import MutationAuthority\n", tmp_path)
        codes = {finding.get("code") for finding in json.loads(completed.stdout or "[]")}
        assert "TID251" not in codes, completed.stdout[:400]

    def test_the_governance_files_are_exempt(self) -> None:
        """`authority.py` must be able to name its own sentinel, or the type could not be
        minted at all — and an exemption that stopped working would break the chokepoint
        rather than tighten it."""
        import tomllib

        config = tomllib.loads((BACKEND_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        ignores = config["tool"]["ruff"]["lint"]["per-file-ignores"]
        assert "TID251" in ignores.get("src/governance/authority.py", []), ignores
        assert "src/governance/**/*.py" not in ignores, (
            "the exemption is file-by-file on purpose: a glob would unban every future name in "
            "the table for every future module in this package (design §2.2.1)"
        )

    def test_every_banned_symbol_this_test_names_is_in_the_table(self) -> None:
        """Keeps this test's own list honest against `pyproject.toml`, so a symbol removed
        from the table cannot leave a passing test behind that no longer checks anything."""
        import tomllib

        config = tomllib.loads((BACKEND_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        table = config["tool"]["ruff"]["lint"]["flake8-tidy-imports"]["banned-api"]
        for module, symbol in BANNED_IMPORTS:
            assert f"{module}.{symbol}" in table, f"{module}.{symbol} is not banned in pyproject.toml"


class TestTheMarkerMakesPrimitivesFindable:
    def test_a_decorated_function_is_marked(self) -> None:
        @mutation_primitive
        def writes_something(authority: MutationAuthority) -> None:
            """A stand-in for a real primitive."""

        assert is_mutation_primitive(writes_something)
        assert getattr(writes_something, MARKER_ATTRIBUTE) is True

    def test_an_undecorated_function_is_not(self) -> None:
        def reads_something() -> None:
            """Not a primitive."""

        assert not is_mutation_primitive(reads_something)

    def test_the_decorator_returns_the_same_function_object(self) -> None:
        """Not a wrapper. §0.4.2's conformance test binds against `inspect.signature`, so a
        wrapper that changed the signature would silently disable the check that catches a
        missing authority argument."""

        def target(authority: MutationAuthority) -> None:
            """A stand-in."""

        assert mutation_primitive(target) is target

    def test_the_signature_is_unchanged(self) -> None:
        import inspect

        def target(authority: MutationAuthority, path: str) -> None:
            """A stand-in."""

        before = inspect.signature(target)
        assert inspect.signature(mutation_primitive(target)) == before

    def test_the_decorator_name_is_exported_for_the_checker(self) -> None:
        """`scripts/check-chokepoint.sh` matches the decorator syntactically, so both sides
        must agree on one spelling. A renamed decorator with the checker still looking for the
        old name is why §2.2.1 makes an EMPTY primitive set a hard error."""
        assert DECORATOR_NAME == mutation_primitive.__name__

    def test_the_decorator_performs_no_validation(self) -> None:
        """Asserted deliberately, so nobody later trusts it for enforcement it cannot provide:
        it cannot know which argument is the authority, nor whether the six stages ran. The
        mint-only type, the banned-api rule and the reachability check are the enforcement."""

        @mutation_primitive
        def takes_no_authority_at_all() -> str:
            """Marked but unauthorised — the decorator does not object, by design."""
            return "ran"

        assert takes_no_authority_at_all() == "ran"
