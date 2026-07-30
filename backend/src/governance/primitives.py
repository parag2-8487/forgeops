# SPDX-License-Identifier: FSL-1.1-ALv2
"""The mutation-primitive marker (design.md §2.2.1, §11.6, Q-03).

What a primitive is
-------------------
A function that changes state outside this process — writes a file, sends a command to an
agent, applies a change set. Every one carries `@mutation_primitive` and takes a
`MutationAuthority` as a required argument, so two independent things become true:

* **Omitting the authority is a call-site error.** §0.4.2's conformance test binds every
  discovered call site against the real signature, so a forgotten argument fails in
  milliseconds rather than in review.
* **The set of primitives is discoverable.** `scripts/check-chokepoint.sh` finds them by
  scanning for this decorator rather than reading a hand-maintained list, so a newly marked
  function is covered the moment it is written. A list would be one edit away from being
  wrong, and wrong in the direction of silence.

Why the decorator does not enforce anything at runtime
------------------------------------------------------
It deliberately does not inspect its arguments or check the authority. Two reasons, and the
second is the important one.

A runtime check would be *weaker* than what already exists: `MutationAuthority` cannot be
constructed outside `governance/`, so possessing one is already proof. Re-verifying it here
would add a branch that looks like the enforcement while the real enforcement is the type.

And a decorator that validated would invite being trusted for validation it cannot perform.
It cannot know which argument is the authority, cannot know whether the change set it names is
the one being applied, and cannot know whether the six stages ran. §2.2.1's three mechanisms —
the mint-only type, the banned-api rule and the reachability check — are what enforce this.
The marker's job is to make the primitive *findable*, and it does exactly that.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final, ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")

#: The attribute the decorator sets. `scripts/check-chokepoint.sh` matches the decorator
#: **syntactically** by name, because it AST-walks source without importing it — importing
#: `backend/src/**` to enumerate primitives would run module-level code in a lint. This
#: attribute is the runtime counterpart, for tests and for Q-03's call-graph walk.
MARKER_ATTRIBUTE: Final[str] = "__forgeops_mutation_primitive__"

#: The decorator's own name, exported so the checker and Q-03 match on one spelling. A
#: renamed decorator with the checker still looking for the old name is the failure mode
#: §2.2.1 closes by making an empty primitive set a hard error.
DECORATOR_NAME: Final[str] = "mutation_primitive"


def mutation_primitive(func: Callable[P, R]) -> Callable[P, R]:
    """Mark `func` as a mutation primitive.

    Returns the function itself rather than a wrapper. A wrapper would change the signature
    `inspect.signature` reports unless it were written with exacting care, and §0.4.2's
    conformance test binds against that signature — so a careless wrapper here would silently
    disable the check that catches a missing authority argument. Setting an attribute is
    enough for both consumers and costs nothing.
    """
    setattr(func, MARKER_ATTRIBUTE, True)
    return func


def is_mutation_primitive(obj: object) -> bool:
    """Whether `obj` carries the marker."""
    return getattr(obj, MARKER_ATTRIBUTE, False) is True


__all__ = ["DECORATOR_NAME", "MARKER_ATTRIBUTE", "is_mutation_primitive", "mutation_primitive"]
