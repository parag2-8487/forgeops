# Service mesh — the decision, and what it costs. §2.7b

**Cilium is the default. Istio Ambient is the documented fallback. Linkerd is avoided.**

## Why Cilium

The deciding constraint is the fleet shape: this platform targets 10,000 self-hosted agents, and a
sidecar-per-pod mesh multiplies that by the sidecar's footprint. Cilium is sidecarless — the datapath is
eBPF in the kernel — so per-pod overhead is close to zero, and Hubble gives flow-level observability
without a second proxy to scrape. `cilium-values.yaml` is the installable form of that choice.

## Why Istio Ambient is the fallback and not the default

Ambient mode removes the per-pod sidecar too (ztunnel per node, waypoint per namespace only when L7 is
needed), so it is not a sidecar-tax choice either. It is the fallback rather than the default because it
buys rich L7 policy and mature multi-cluster at the cost of two more components to run. A deployment that
needs L7 authorisation or cross-cluster identity should switch; one that does not should not pay for it.
`istio-ambient-values.yaml` is that path, kept current so switching is a values change rather than a
project.

## Why Linkerd is avoided

Linkerd's **stable** releases moved behind a Buoyant subscription in 2024. The edge releases remain open,
but "run edge in production" is not advice this project will give, and a control plane whose supported
builds require a commercial agreement is the wrong dependency for a platform whose whole pitch is
self-hosting on your own terms. This is a licensing judgement, not a technical one: Linkerd's technical
record is good.

## What is NOT claimed here

Neither mesh is installed by this repository, and nothing in the agent's operation catalogue installs one.
These files are the reviewed configuration an operator applies with Helm; the decision is recorded so the
next person does not re-litigate it, and `tests/meta/test_service_mesh_decision.py` asserts the files stay
consistent with the decision rather than drifting into prose nobody checks.
