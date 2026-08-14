# SPDX-License-Identifier: FSL-1.1-ALv2
"""Q-03 — the chokepoint is unbypassable (design.md §2.2.1, §11.6, Appendix B Q-03).

Property, universally quantified:

    ∀ generated call graphs over `src/**`: no `@mutation_primitive` is reachable without a
    `MutationAuthority`, and `MutationAuthority` cannot be constructed outside `governance/`;
    ∀ Go packages: `executor/internal/mutate` has no importer outside `executor/**`.

Why this is a property and not three example tests
--------------------------------------------------
§2.2.1's claim is not "today's tree is clean" — `scripts/check-chokepoint.sh` asserts that, and
leaf 7.3 wired it into two CI jobs and `pre-commit`. The claim Q-03 quantifies is stronger and
different: **the checker's answer is correct for every call graph**, not only for the one shape
the current tree happens to have. A gate that is right about one tree and wrong about the next
refactor is a gate that will be wrong exactly once, at the worst moment.

So this file generates the input rather than reading it. `hypothesis` builds module trees with a
primitive and a mix of call sites — authorised, unauthorised, inside `governance/`, on typed
non-owner receivers, on untypable receivers — and asserts the analysis's verdict equals the
ground truth the generator knows by construction. The same for Go: generated package graphs with
importers inside and outside the executor subtree.

The three clauses, and which mechanism each one guards
------------------------------------------------------
* **Clause A — reachability (Python).** Over generated call graphs, `find_primitive_calls`
  authorises exactly the call sites that are inside `governance/` or hold an authority.
  Guards §2.2.1 mechanism 3's Python half.
* **Clause B — the capability type.** `MutationAuthority` cannot be constructed with any
  sentinel but the module-private one, for **every** value hypothesis can produce. Guards
  mechanism 1. This is the clause the negative control breaks.
* **Clause C — the compiler-enforced boundary (Go).** Over generated package graphs,
  `classify_importers` reports exactly the importers outside `executor/**`; and over the REAL
  graph, there are none. Guards mechanism 3's Go half.

Each clause also carries one assertion against the real tree, because a property over generated
inputs proves the analysis and says nothing about the codebase it is pointed at.

Negative control (`mutations.toml` Q-03): delete the `_MINT_SENTINEL` check in
`MutationAuthority.__post_init__`. Clause B must then fail.
"""

from __future__ import annotations

import importlib.util
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from src.governance.authority import FORGERY_MESSAGE, MutationAuthority, mint_authority

pytestmark = pytest.mark.mandatory

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_SRC = REPO_ROOT / "backend" / "src"
AGENT_ROOT = REPO_ROOT / "agent"
ANALYSIS_MODULE = REPO_ROOT / "scripts" / "chokepoint_graph.py"

_SETTINGS = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)


def _load_analysis() -> ModuleType:
    """Import the checker's analysis by path.

    The SAME module `scripts/check-chokepoint.sh` runs, not a reimplementation. That is the
    Q-06/Q-14 lesson applied to a lint: two implementations of one rule agree until the day
    they do not, and then the gate and the property disagree about what "reachable" means.
    """
    spec = importlib.util.spec_from_file_location("forgeops_q03_chokepoint_graph", ANALYSIS_MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ANALYSIS = _load_analysis()


# ─── generators ───────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CallSite:
    """One generated call site, with the verdict the generator knows it must receive."""

    kind: str
    expected_verdict: str

    def render(self, index: int) -> str:
        name = f"caller_{index}"
        match self.kind:
            case "authorised-by-parameter":
                return textwrap.dedent(f"""
                    def {name}(writer: Writer, authority: MutationAuthority) -> None:
                        writer.append("payload", authority=authority)
                """)
            case "authorised-by-mint":
                return textwrap.dedent(f"""
                    def {name}(writer: Writer) -> None:
                        minted = mint_authority()
                        writer.append("payload", minted)
                """)
            case "unauthorised":
                return textwrap.dedent(f"""
                    def {name}(writer: Writer) -> None:
                        writer.append("payload")
                """)
            case "unauthorised-none":
                return textwrap.dedent(f"""
                    def {name}(writer: Writer) -> None:
                        authority = None
                        writer.append("payload", authority=authority)
                """)
            case "untypable-receiver":
                return textwrap.dedent(f"""
                    def {name}(anything) -> None:
                        anything.append("payload")
                """)
            case "typed-non-owner":
                return textwrap.dedent(f"""
                    def {name}(collected: list[str]) -> None:
                        collected.append("payload")
                """)
            case "literal-non-owner":
                return textwrap.dedent(f"""
                    def {name}() -> list[str]:
                        collected = []
                        collected.append("payload")
                        return collected
                """)
            case _:  # pragma: no cover - a generator that produced an unknown kind
                raise AssertionError(self.kind)


#: Every generated call-site shape, with the verdict the analysis must return for it.
#:
#: `typed-non-owner` and `literal-non-owner` expect **no verdict at all** — the analysis must
#: not classify them as primitive calls, because they are `list.append`. That is the clause
#: that makes the check usable rather than merely loud, so it is generated rather than asserted
#: once.
CALL_SITE_KINDS = (
    CallSite("authorised-by-parameter", "authority"),
    CallSite("authorised-by-mint", "authority"),
    CallSite("unauthorised", "no-authority"),
    CallSite("unauthorised-none", "no-authority"),
    CallSite("untypable-receiver", "unresolved-receiver"),
    CallSite("typed-non-owner", ""),
    CallSite("literal-non-owner", ""),
)

PRELUDE = textwrap.dedent('''
    """Generated module for Q-03. Never imported; only parsed."""

    from dataclasses import dataclass


    def mutation_primitive(func):
        return func


    @dataclass(frozen=True)
    class MutationAuthority:
        change_set_id: str


    def mint_authority() -> MutationAuthority:
        return MutationAuthority(change_set_id="generated")


    class Writer:
        @mutation_primitive
        def append(self, payload: str, authority: MutationAuthority | None = None) -> None:
            pass
''')


def _write_module(root: Path, package: str, sites: list[CallSite]) -> Path:
    """Render one generated module of call sites into `root/<package>/generated.py`."""
    directory = root / package if package else root
    directory.mkdir(parents=True, exist_ok=True)
    body = PRELUDE + "\n" + "\n".join(site.render(index) for index, site in enumerate(sites))
    path = directory / "generated.py"
    path.write_text(body, encoding="utf-8")
    return path


call_sites = st.lists(st.sampled_from(CALL_SITE_KINDS), min_size=1, max_size=8)
packages = st.sampled_from(("governance", "audit", "generation", "websocket", "projects"))


# ─── clause A — reachability over generated call graphs ───────────────────────────────────


class TestClauseAReachabilityOverGeneratedCallGraphs:
    @_SETTINGS
    @given(sites=call_sites, package=packages)
    def test_the_analysis_authorises_exactly_the_right_call_sites(
        self, tmp_path_factory: pytest.TempPathFactory, sites: list[CallSite], package: str
    ) -> None:
        """The core clause: verdict equals ground truth, for every generated graph.

        `package` matters because §2.2.1 authorises a call by position when it is lexically
        inside `governance/`. Generating the same call shapes in and out of that package is what
        turns "governance is exempt" from a branch nobody exercises into a quantified claim.
        """
        root = tmp_path_factory.mktemp("q03")
        _write_module(root, package, sites)

        primitives, calls = ANALYSIS.analyse(root)
        assert primitives, "the generated module carries a primitive; discovery found none"

        by_line = {call.line: call.verdict for call in calls}
        source = (root / package / "generated.py").read_text(encoding="utf-8").splitlines()

        for index, site in enumerate(sites):
            line = _call_line(source, index)
            expected = _expected_verdict(site, package)
            if expected is None:
                assert line not in by_line, (
                    f"{site.kind} at line {line} was classified as a primitive call "
                    f"({by_line.get(line)}); a list.append must never be reported"
                )
                continue
            assert line in by_line, f"{site.kind} at line {line} was not classified at all"
            assert by_line[line] == expected, (
                f"{site.kind} in package {package!r} got verdict {by_line[line]!r}, expected {expected!r}"
            )

    @_SETTINGS
    @given(sites=call_sites)
    def test_no_unauthorised_call_is_ever_reported_as_authorised(
        self, tmp_path_factory: pytest.TempPathFactory, sites: list[CallSite]
    ) -> None:
        """The one-directional half, stated separately because it is the half that matters.

        A false negative here is a mutation primitive reachable without authority that the gate
        calls clean. Outside `governance/`, the number of authorised verdicts must never exceed
        the number of call sites the generator built with an authority.
        """
        root = tmp_path_factory.mktemp("q03")
        _write_module(root, "generation", sites)
        _, calls = ANALYSIS.analyse(root)
        authorised = [call for call in calls if call.authorised]
        expected = sum(1 for site in sites if site.expected_verdict == "authority")
        assert len(authorised) == expected, (
            f"{len(authorised)} call site(s) were authorised but only {expected} carry an authority"
        )

    @_SETTINGS
    @given(sites=call_sites)
    def test_governance_position_authorises_every_shape(
        self, tmp_path_factory: pytest.TempPathFactory, sites: list[CallSite]
    ) -> None:
        """Inside `governance/`, every RESOLVED call site is authorised by position.

        An untypable receiver is still refused, even inside `governance/`: the analysis cannot
        tell whether that call reaches a primitive at all, and "we cannot tell" on a mutation
        path is refusal (§9's convention).
        """
        root = tmp_path_factory.mktemp("q03")
        _write_module(root, "governance", sites)
        _, calls = ANALYSIS.analyse(root)
        for call in calls:
            assert call.verdict in ("governance", "unresolved-receiver"), call

    def test_the_real_backend_tree_has_no_unauthorised_call(self) -> None:
        """The tree itself, not a generated one. Both halves of the claim need this."""
        primitives, calls = ANALYSIS.analyse(BACKEND_SRC)
        assert primitives, "the primitive set is empty; §2.2.1 makes that a hard failure"
        offenders = [call.render() for call in calls if not call.authorised]
        assert not offenders, offenders

    def test_the_real_tree_reaches_the_primitive_from_governance_only(self) -> None:
        """`AuditWriter.append` is called only from inside `governance/`.

        Two call sites since leaf 8.1: the chokepoint's transit record, and D-70's
        device-lifecycle recorder. The assertion is on the **package**, not on a file list — a
        file list would have to be edited by every leaf that adds a governance module, and an
        assertion people edit routinely stops being read.
        """
        _, calls = ANALYSIS.analyse(BACKEND_SRC)
        assert calls, "nothing reaches the primitive; the clause would be vacuous"
        for call in calls:
            assert call.path.startswith("src/governance/"), call.render()
            assert call.verdict == "governance", call.render()


def _expected_verdict(site: CallSite, package: str) -> str | None:
    """The verdict the generator knows this site must receive in this package.

    `None` means "must not be classified as a primitive call at all" — the `list.append` cases.

    An `unresolved-receiver` stays unresolved even inside `governance/`, and that ordering is the
    point: the analysis cannot tell whether an untypable receiver reaches a primitive, so its
    position cannot authorise it. Position authorises a call the analysis has already decided IS
    a primitive call.
    """
    if not site.expected_verdict:
        return None
    if site.expected_verdict == "unresolved-receiver":
        return "unresolved-receiver"
    return "governance" if package == "governance" else site.expected_verdict


def _call_line(source: list[str], index: int) -> int:
    """The 1-based line of the `append(`/`dispatch_apply(` call inside `caller_<index>`."""
    marker = f"def caller_{index}("
    start = next(number for number, text in enumerate(source, 1) if marker in text)
    for number in range(start, len(source) + 1):
        text = source[number - 1]
        if ".append(" in text or "dispatch_apply(" in text:
            return number
    raise AssertionError(f"no call found in caller_{index}")


# ─── clause B — the capability type ───────────────────────────────────────────────────────


class TestClauseBTheCapabilityTypeCannotBeForged:
    """The clause the negative control breaks. Everything else rests on it."""

    @_SETTINGS
    @given(
        sentinel=st.one_of(
            st.none(),
            st.booleans(),
            st.integers(),
            st.text(max_size=20),
            st.binary(max_size=20),
            st.builds(object),
            st.lists(st.integers(), max_size=3),
            st.dictionaries(st.text(max_size=3), st.integers(), max_size=3),
            st.tuples(st.integers()),
        )
    )
    def test_no_value_but_the_module_private_sentinel_can_mint(self, sentinel: object) -> None:
        """∀ sentinels: construction raises `TypeError` with the contract's message.

        Quantified over values rather than asserted once, because the check is an **identity**
        comparison and the failure modes people reach for are all equality-shaped: a string that
        looks right, an `object()` of their own, a truthy value.
        """
        with pytest.raises(TypeError, match="may only be minted"):
            MutationAuthority(
                change_set_id=_UUID,
                approval_id=_UUID,
                policy_bundle_digest="sha256:" + "00" * 32,
                blast_radius="workspace",
                audit_seq=1,
                envelope_digest="d" * 64,
                _sentinel=sentinel,
            )

    def test_the_error_names_the_contract_rather_than_a_restated_string(self) -> None:
        """The message is exported, so the test asserts the contract and not its own copy."""
        with pytest.raises(TypeError) as raised:
            MutationAuthority(
                change_set_id=_UUID,
                approval_id=_UUID,
                policy_bundle_digest="d",
                blast_radius="workspace",
                audit_seq=1,
                envelope_digest="d",
                _sentinel=object(),
            )
        assert str(raised.value) == FORGERY_MESSAGE

    def test_the_mint_is_the_only_thing_that_produces_one(self) -> None:
        authority = mint_authority(
            change_set_id=_UUID,
            approval_id=_UUID,
            policy_bundle_digest="sha256:" + "00" * 32,
            blast_radius="workspace",
            audit_seq=1,
            envelope_digest="d" * 64,
        )
        assert isinstance(authority, MutationAuthority)

    @_SETTINGS
    @given(audit_seq=st.integers(max_value=0))
    def test_an_authority_cannot_predate_its_audit_record(self, audit_seq: int) -> None:
        """`audit_seq` is the sequence of a WRITTEN record, so a non-positive value is a mint
        for a transit that wrote nothing — which is the one invariant Q-04 rests on."""
        with pytest.raises(ValueError, match="positive sequence number"):
            mint_authority(
                change_set_id=_UUID,
                approval_id=_UUID,
                policy_bundle_digest="sha256:" + "00" * 32,
                blast_radius="workspace",
                audit_seq=audit_seq,
                envelope_digest="d" * 64,
            )

    def test_the_sentinel_is_not_importable_by_name_anywhere_but_its_own_module(self) -> None:
        """Mechanism 2. The identity check is only unforgeable while the name is unreachable.

        Asserted over the real tree by parsing rather than by trusting Ruff to have run: a
        banned-api entry is a lint, and a lint that was not run is not a boundary. Leaf 8.1
        found that it is worse than that — a lint that WAS run is not a boundary either, once
        any file carries a per-file ignore for its rule (finding 55) — so this clause now drives
        the same `CONFINED_NAMES` table `check-chokepoint.sh` uses, over every confined name
        rather than over `_MINT_SENTINEL` alone.
        """
        violations = ANALYSIS.find_confinement_violations(BACKEND_SRC)
        assert not violations, [violation.render() for violation in violations]

    def test_every_name_ruff_confines_is_also_confined_by_the_parse(self) -> None:
        """The two mechanisms must agree, or the weaker one is the real boundary.

        Reads `pyproject.toml`'s `banned-api` table and asserts that every §2.2.1 entry naming a
        SYMBOL — the entries of the form `src.<module>.<name>` — appears in `CONFINED_NAMES`.
        Without this, a sixth banned name added to the lint would be silently outside the parse,
        and the parse is the half that cannot be switched off.
        """
        import tomllib

        table = tomllib.loads((REPO_ROOT / "backend" / "pyproject.toml").read_text(encoding="utf-8"))
        banned = table["tool"]["ruff"]["lint"]["flake8-tidy-imports"]["banned-api"]
        parsed = {entry.name for entry in ANALYSIS.CONFINED_NAMES}
        # A symbol entry is one whose last segment is not a package or module in `src/`: the
        # module bans are `src.ai`, `src.projects`, …; the symbol bans carry a third-or-deeper
        # segment naming an identifier, e.g. `src.governance.envelope.sign_envelope`.
        symbol_entries = {
            key.rsplit(".", 1)[1]
            for key in banned
            if key.startswith("src.") and not (BACKEND_SRC / Path(*key.split(".")[1:])).is_dir()
            if not (BACKEND_SRC / Path(*key.split(".")[1:])).with_suffix(".py").is_file()
        }
        missing = sorted(symbol_entries - parsed)
        assert not missing, (
            f"banned-api confines {missing} but CONFINED_NAMES does not, so those names are "
            "protected only by a lint that any per-file ignore disables (finding 55)"
        )

    @_SETTINGS
    @given(
        confined=st.sampled_from(range(len(ANALYSIS.CONFINED_NAMES))),
        offender=st.sampled_from(("ai.routes", "projects.service", "secrets.routes", "audit.writer")),
    )
    def test_a_generated_module_reaching_a_confined_name_is_always_reported(
        self, tmp_path_factory: pytest.TempPathFactory, confined: int, offender: str
    ) -> None:
        """The control of the control: the parse must FAIL on a violation, for every name.

        A confinement check that reports nothing on a clean tree is indistinguishable from one
        that reports nothing at all. This generates the violation — every confined name × several
        offending modules, in both the import and the attribute form — and requires it to be
        found. `audit.writer` is in the offender set on purpose: the module that owns the one
        mutation primitive is not thereby permitted to name the signing surface.
        """
        entry = ANALYSIS.CONFINED_NAMES[confined]
        assert offender not in entry.permitted, "the generator must produce a real violation"
        root = tmp_path_factory.mktemp("q03-confinement")
        package, _, module = offender.rpartition(".")
        directory = root / package.replace(".", "/")
        directory.mkdir(parents=True, exist_ok=True)
        owner_package = entry.owner.rsplit(".", 1)[0]
        (directory / f"{module}.py").write_text(
            f"from ..{entry.owner} import {entry.name}\n"
            f"from .. import {owner_package} as _pkg\n"
            f"_reached = _pkg.{entry.name}\n",
            encoding="utf-8",
        )
        violations = ANALYSIS.find_confinement_violations(root)
        assert violations, f"{offender} reaching {entry.name} was not reported"
        assert {violation.name for violation in violations} == {entry.name}
        kinds = {violation.kind for violation in violations}
        assert "import" in kinds
        if entry.check_attribute:
            assert "attribute" in kinds

    @_SETTINGS
    @given(confined=st.sampled_from(range(len(ANALYSIS.CONFINED_NAMES))))
    def test_a_permitted_module_reaching_the_same_name_is_never_reported(
        self, tmp_path_factory: pytest.TempPathFactory, confined: int
    ) -> None:
        """The other direction. A check that reported the owner would be switched off in a week."""
        entry = ANALYSIS.CONFINED_NAMES[confined]
        root = tmp_path_factory.mktemp("q03-confinement-ok")
        for permitted in sorted(entry.permitted):
            package, _, module = permitted.rpartition(".")
            directory = root / package.replace(".", "/") if package else root
            directory.mkdir(parents=True, exist_ok=True)
            (directory / f"{module}.py").write_text(
                f"from ..{entry.owner} import {entry.name}\n_reached = {entry.name}\n",
                encoding="utf-8",
            )
        assert ANALYSIS.find_confinement_violations(root) == []

    def test_the_authority_is_frozen_and_slotted(self) -> None:
        """A handler that could widen `blast_radius` after the fact would make every downstream
        check advisory, and an attribute a caller could add is a place to smuggle state.

        The two refusals raise **different** exception types, which is worth knowing rather than
        hiding behind a tuple. Measured on CPython 3.13.3:

        * assigning an existing field raises `dataclasses.FrozenInstanceError` — what `frozen=True`
          promises;
        * assigning a NEW attribute raises `TypeError` ("super(type, obj): obj ... is not an
          instance or subtype of type"), because `slots=True` rebuilds the class and the frozen
          `__setattr__` closes over the pre-rebuild one.

        Both refuse, so the guarantee holds. But code written as `except FrozenInstanceError`
        around a `setattr` on this type would silently miss the second case, so the asymmetry is
        asserted rather than smoothed over.
        """
        import dataclasses

        authority = mint_authority(
            change_set_id=_UUID,
            approval_id=_UUID,
            policy_bundle_digest="sha256:" + "00" * 32,
            blast_radius="read_only",
            audit_seq=1,
            envelope_digest="d" * 64,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            authority.blast_radius = "infrastructure"  # type: ignore[misc]
        with pytest.raises(TypeError):
            authority.smuggled = "value"  # type: ignore[attr-defined]
        assert not hasattr(authority, "__dict__"), "slots=True must leave no instance dict"


# ─── clause C — the Go boundary ───────────────────────────────────────────────────────────

INSIDE = ANALYSIS.GO_EXECUTOR_PREFIX
OUTSIDE_PACKAGES = (
    "github.com/parag8487/ForgeOps/agent/internal/session",
    "github.com/parag8487/ForgeOps/agent/internal/scanner",
    "github.com/parag8487/ForgeOps/agent/cmd/forgeops-agent",
    "github.com/parag8487/ForgeOps/agent/internal/executorish",
)
INSIDE_PACKAGES = (
    INSIDE,
    f"{INSIDE}/internal/mutate",
    f"{INSIDE}/dispatch",
)


class TestClauseCTheGoBoundaryHasNoOutsideImporter:
    @_SETTINGS
    @given(
        outside=st.lists(st.sampled_from(OUTSIDE_PACKAGES), unique=True),
        inside=st.lists(st.sampled_from(INSIDE_PACKAGES), unique=True),
    )
    def test_exactly_the_outside_importers_are_reported(self, outside: list[str], inside: list[str]) -> None:
        """∀ generated package graphs: the offender set equals the outside-importer set.

        `internal/executorish` is in the generated set on purpose. It shares the prefix
        `internal/executor` as a **string** but is a different package, so a prefix test written
        without the boundary `/` would wave it through — and Go would refuse to compile it. The
        generator includes it so the property notices if the check ever becomes that lenient.
        """
        graph: dict[str, list[str]] = {ANALYSIS.GO_MUTATE_PACKAGE: []}
        for package in outside + inside:
            graph[package] = [ANALYSIS.GO_MUTATE_PACKAGE]

        importers, offenders = ANALYSIS.classify_importers(graph)
        reported = {entry.importer for entry in importers if not entry.permitted}
        assert reported == set(outside), f"reported {sorted(reported)}, expected {sorted(outside)}"
        assert len(offenders) == len(outside)
        assert {entry.importer for entry in importers} == set(outside) | set(inside)

    @_SETTINGS
    @given(other=st.lists(st.sampled_from(OUTSIDE_PACKAGES + INSIDE_PACKAGES), unique=True))
    def test_a_package_that_does_not_import_the_boundary_is_never_reported(self, other: list[str]) -> None:
        graph: dict[str, list[str]] = {ANALYSIS.GO_MUTATE_PACKAGE: []}
        for package in other:
            graph[package] = ["fmt", "errors"]
        importers, offenders = ANALYSIS.classify_importers(graph)
        assert importers == [] and offenders == []

    def test_a_vacuous_graph_is_refused_rather_than_passed(self) -> None:
        """The guard that stops clause C being true because the query returned nothing."""
        with pytest.raises(RuntimeError, match="import graph is empty"):
            ANALYSIS.classify_importers({})
        with pytest.raises(RuntimeError, match="does not contain"):
            ANALYSIS.classify_importers({"example.com/a": ["example.com/b"]})

    @pytest.mark.skipif(not (AGENT_ROOT / "go.mod").is_file(), reason="the agent module is absent")
    def test_the_real_agent_module_has_no_outside_importer(self) -> None:
        """The real graph, from `go list -deps -json ./...`.

        Not skipped when Go is missing — `pytest.importorskip` has no analogue for a toolchain,
        and a silent skip here is exactly what §0.4.4 forbids. If `go` is absent the call raises
        and this test fails, which is the honest outcome: the claim was not checked.
        """
        importers, offenders = ANALYSIS.check_go_boundary(AGENT_ROOT)
        assert offenders == [], offenders
        assert all(entry.permitted for entry in importers)


_UUID = __import__("uuid").UUID("00000000-0000-4000-8000-000000000000")
