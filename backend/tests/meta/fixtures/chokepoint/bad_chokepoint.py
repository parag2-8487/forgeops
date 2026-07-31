# SPDX-License-Identifier: FSL-1.1-ALv2
"""Negative fixture for the Python half of check-chokepoint (design.md §2.2.1, Q-03).

Every function below is an OFFENDER, and each one is a different way to reach a mutation
primitive without a `MutationAuthority`. `test_check_chokepoint.py` asserts that each is
reported, so no branch of the checker can rot into a no-op.

This file is a fixture, never imported by production code, and it deliberately does **not**
live under `backend/src/` — the checker walks that tree, and a permanent offender there would
red the build. It is walked explicitly by the meta test with this directory as the source root.
"""

from __future__ import annotations

from dataclasses import dataclass


def mutation_primitive(func):  # noqa: ANN001, ANN201 - a fixture stand-in for the real marker
    """A stand-in with the same NAME, because the checker matches the decorator syntactically."""
    return func


@dataclass(frozen=True)
class MutationAuthority:
    """A stand-in with the same name, so annotation-based resolution has something to bind."""

    change_set_id: str


def mint_authority() -> MutationAuthority:
    return MutationAuthority(change_set_id="fixture")


class Writer:
    """Owns a primitive, so receiver resolution has an owning class to resolve to."""

    @mutation_primitive
    def append(self, payload: str, authority: MutationAuthority | None = None) -> None:  # noqa: D102 - fixture
        pass


def offender_bare_call_without_authority(writer: Writer) -> None:
    """OFFENDER: a resolved receiver, outside governance/, and no authority passed."""
    writer.append("no authority anywhere")


def offender_authority_named_but_not_typed(writer: Writer) -> None:
    """OFFENDER: `authority` is a bare `None`, not a `MutationAuthority`.

    This is the case a keyword-name heuristic would wave through, and it is the exact failure
    §11.6 says the capability type replaces: "someone forgot to call `assert_authorized()`".
    """
    authority = None
    writer.append("pretending", authority=authority)


def offender_unresolved_receiver(anything) -> None:  # noqa: ANN001 - the point of the fixture
    """OFFENDER: the receiver has no annotation, so the checker cannot type it.

    Reported as `unresolved-receiver` and blocking, because a receiver that might be the
    primitive's owner is not something a mutation-path check may assume away.
    """
    anything.append("could be a Writer, could be a list")


def offender_module_level_primitive_call() -> None:
    """OFFENDER: a module-level primitive reached by bare name with no authority."""
    dispatch_apply(device_id="d-1")


@mutation_primitive
def dispatch_apply(*, device_id: str, authority: MutationAuthority | None = None) -> None:  # noqa: D103 - fixture
    pass


def not_an_offender_list_append() -> list[str]:
    """CLEAN: a literal-typed receiver. Must NOT be reported, or the check is unusable.

    `AuditWriter.append` and `list.append` share a name. If this were flagged, the real check
    would report hundreds of false positives in `backend/src/**` and get switched off.
    """
    collected = []
    collected.append("this is a list, not a Writer")
    return collected


def not_an_offender_with_authority(writer: Writer, authority: MutationAuthority) -> None:
    """CLEAN: an annotated `MutationAuthority` parameter, passed to the primitive."""
    writer.append("authorised", authority=authority)


def not_an_offender_with_minted_authority(writer: Writer) -> None:
    """CLEAN: a local assigned from `mint_authority(...)` holds an authority."""
    minted = mint_authority()
    writer.append("authorised", minted)


def not_an_offender_typed_non_owner(other: dict[str, str]) -> None:
    """CLEAN: a resolved receiver that is not the owning class is not a primitive call."""
    other.update({"append": "not the primitive"})
