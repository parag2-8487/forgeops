# SPDX-License-Identifier: FSL-1.1-ALv2
"""`Principal` and identity-derived blast radius (design.md §11.2, §17.1 D-39).

The assertions that matter are the ones about what CANNOT be done:

* `Principal.for_user` takes no `blast_radius` argument, so no call site can widen its
  own authority. That is the failure D-39 closes, and it would look entirely reasonable
  in review if the parameter existed.
* `Principal` is frozen, so a handler cannot promote a viewer after verification.
* A device's radius is the NARROWEST of its attestation and its project grant, never
  the widest, and an unrecognised attestation resolves to `read_only` rather than
  raising — a device presenting an attestation this build does not know is exactly the
  one that must not get write authority.
* No attestation kind Phase 1 ships reaches `infrastructure`, because Phase 1 has no
  hardware-rooted device attestation (§14.3 says so plainly).
"""

from __future__ import annotations

import dataclasses
import inspect
import uuid

import pytest
from src.auth.models import UserRole
from src.auth.principal import (
    BLAST_RADIUS_ORDER,
    DEVICE_ATTESTATION_BLAST_RADIUS,
    ROLE_BLAST_RADIUS,
    Principal,
    blast_radius_for_device,
    blast_radius_for_role,
    narrowest,
    widest,
)

pytestmark = pytest.mark.mandatory


class TestRoleDerivedBlastRadius:
    @pytest.mark.parametrize(
        ("role", "expected"),
        [
            (UserRole.VIEWER, "read_only"),
            (UserRole.DEVELOPER, "workspace"),
            (UserRole.ADMIN, "infrastructure"),
        ],
    )
    def test_each_role_maps_to_its_radius(self, role: UserRole, expected: str) -> None:
        assert blast_radius_for_role(role) == expected

    def test_every_role_is_mapped(self) -> None:
        """A role with no mapping would raise `KeyError` at verification time, i.e. a
        500 on every request from that role."""
        assert set(ROLE_BLAST_RADIUS) == set(UserRole)

    def test_a_viewer_cannot_reach_workspace(self) -> None:
        assert BLAST_RADIUS_ORDER.index(blast_radius_for_role(UserRole.VIEWER)) < (
            BLAST_RADIUS_ORDER.index("workspace")
        )


class TestTheConstructorCannotWidenAuthority:
    def test_for_user_accepts_no_blast_radius_argument(self) -> None:
        """The structural assertion. If this parameter is ever added, a call site can
        grant itself infrastructure authority and the code will read as if it were
        configuration."""
        parameters = inspect.signature(Principal.for_user).parameters
        assert "blast_radius" not in parameters, sorted(parameters)

    def test_for_device_accepts_no_blast_radius_argument(self) -> None:
        parameters = inspect.signature(Principal.for_device).parameters
        assert "blast_radius" not in parameters, sorted(parameters)

    def test_the_principal_is_frozen(self) -> None:
        principal = Principal.for_user(
            user_id=uuid.uuid4(),
            subject=str(uuid.uuid4()),
            email="viewer@example.invalid",
            role=UserRole.VIEWER,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            object.__setattr__  # noqa: B018 - referenced so the intent is unmistakable
            principal.blast_radius = "infrastructure"  # type: ignore[misc]

    def test_the_role_cannot_be_promoted_after_verification(self) -> None:
        principal = Principal.for_user(
            user_id=uuid.uuid4(),
            subject=str(uuid.uuid4()),
            email="viewer@example.invalid",
            role=UserRole.VIEWER,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            principal.role = UserRole.ADMIN  # type: ignore[misc]

    def test_a_user_principal_gets_the_radius_its_role_implies(self) -> None:
        for role, expected in ROLE_BLAST_RADIUS.items():
            principal = Principal.for_user(
                user_id=uuid.uuid4(),
                subject=str(uuid.uuid4()),
                email="who@example.invalid",
                role=role,
            )
            assert principal.blast_radius == expected
            assert principal.kind == "user"
            assert principal.device_id is None


class TestDeviceDerivedBlastRadius:
    def test_a_paired_device_reaches_workspace(self) -> None:
        assert blast_radius_for_device(attestation="paired_device") == "workspace"

    def test_an_unknown_attestation_falls_to_read_only(self) -> None:
        """Fail closed. A device presenting an attestation this build does not
        recognise is exactly the one that must not be granted write authority."""
        assert blast_radius_for_device(attestation="something-new") == "read_only"

    def test_the_result_is_the_narrowest_of_the_two(self) -> None:
        """A widely-attested device on a narrowly-granted project cannot exceed the
        project."""
        assert blast_radius_for_device(attestation="paired_device", project_grant="read_only") == "read_only"

    def test_a_project_grant_cannot_widen_a_weak_attestation(self) -> None:
        assert blast_radius_for_device(attestation="unattested", project_grant="infrastructure") == "read_only"

    def test_no_shipped_attestation_reaches_infrastructure(self) -> None:
        """§14.3: Phase 1 has no hardware-rooted device attestation. A device that
        needs infrastructure authority gets it from an approved change-set carrying a
        minted authority, never from its own identity."""
        assert "infrastructure" not in set(DEVICE_ATTESTATION_BLAST_RADIUS.values())

    def test_a_device_principal_acts_on_its_own_behalf(self) -> None:
        """`user_id` is the device id. Inventing a synthetic user would make the audit
        log claim a person did it."""
        device_id = uuid.uuid4()
        principal = Principal.for_device(device_id=device_id, subject=str(device_id), attestation="paired_device")
        assert principal.kind == "device"
        assert principal.device_id == device_id
        assert principal.user_id == device_id
        assert principal.session_id is None


class TestRadiusOrdering:
    def test_the_order_is_widest_last(self) -> None:
        assert BLAST_RADIUS_ORDER == ("read_only", "workspace", "infrastructure")

    def test_widest_and_narrowest_are_consistent(self) -> None:
        for left in BLAST_RADIUS_ORDER:
            for right in BLAST_RADIUS_ORDER:
                assert BLAST_RADIUS_ORDER.index(widest(left, right)) >= BLAST_RADIUS_ORDER.index(narrowest(left, right))

    def test_narrowest_of_one_is_itself(self) -> None:
        for radius in BLAST_RADIUS_ORDER:
            assert narrowest(radius) == radius
            assert widest(radius) == radius
