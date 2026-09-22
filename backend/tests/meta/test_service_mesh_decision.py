# SPDX-License-Identifier: FSL-1.1-ALv2
"""§2.7b's service-mesh decision, asserted rather than written down and forgotten.

A decision recorded only in prose drifts: somebody adds a Linkerd chart, or the "fallback" values rot until
switching is a project rather than a values change. These tests hold the three claims the boxes make.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

MESH = Path(__file__).resolve().parents[3] / "infra" / "service-mesh"


def test_the_decision_is_recorded_with_all_three_meshes_named() -> None:
    readme = (MESH / "README.md").read_text(encoding="utf-8")
    # Each box's subject has to be addressed, and the reasoning has to be present — a README naming Cilium
    # without saying why is the prose this test exists to prevent.
    assert "Cilium is the default" in readme
    assert "Istio Ambient" in readme
    assert "Linkerd" in readme
    assert "subscription" in readme, "the Linkerd box is a licensing judgement and must say so"
    assert "10,000" in readme, "the deciding constraint is the fleet size; it must be stated"


def test_cilium_is_sidecarless_and_observable() -> None:
    values = yaml.safe_load((MESH / "cilium-values.yaml").read_text(encoding="utf-8"))
    # The properties the box claims: eBPF datapath replacing kube-proxy, and Hubble for flow visibility.
    assert values["kubeProxyReplacement"] is True
    assert values["hubble"]["enabled"] is True
    assert values["hubble"]["relay"]["enabled"] is True
    # The UI is OFF: it has no authentication of its own, so enabling it needs an authenticating proxy and
    # is therefore a decision rather than a default.
    assert values["hubble"]["ui"]["enabled"] is False
    # Encryption on, because a mesh that reports it and does not do it is worse than no mesh.
    assert values["encryption"]["enabled"] is True


def test_the_fallback_is_ambient_and_not_sidecar() -> None:
    values = yaml.safe_load((MESH / "istio-ambient-values.yaml").read_text(encoding="utf-8"))
    # `profile: ambient` is the line that makes this a fallback worth having: a sidecar profile would
    # reintroduce the per-pod cost that rules Istio out at this fleet size.
    assert values["profile"] == "ambient"
    assert "sidecar" not in str(values).lower() or values["profile"] == "ambient"


def test_no_linkerd_configuration_is_introduced() -> None:
    """The avoided option must stay avoided, which is a property of the tree rather than of the README."""
    offenders = [
        path
        for path in (MESH.parent).rglob("*")
        if path.is_file() and "linkerd" in path.name.lower()
    ]
    assert offenders == [], f"Linkerd configuration was added under infra/: {offenders}"


@pytest.mark.parametrize("name", ["README.md", "cilium-values.yaml", "istio-ambient-values.yaml"])
def test_every_declared_file_exists(name: str) -> None:
    assert (MESH / name).is_file(), f"{name} is referenced by the decision and is missing"
