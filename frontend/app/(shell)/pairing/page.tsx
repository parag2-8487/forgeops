// SPDX-License-Identifier: FSL-1.1-ALv2
import { NotImplemented } from "@/components/ui/not-implemented";

export default function PairingPage() {
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold tracking-tight">Agent pairing</h1>
      <NotImplemented
        feature="Agent pairing and attestation status"
        owner="Phase 1 deliverable 1.1 for the pairing protocol; a device inventory view is later work"
        reason="Pairing is agent-initiated and write-only: the endpoints mint and exchange a pairing code. There is no endpoint that reports which devices are paired, so a status panel has nothing to read."
        detail={
          <div className="space-y-2 text-muted-foreground">
            <p>
              The protocol is implemented and tested — single-use codes, a five-attempt cap, per-IP
              and global rate limits, five-minute expiry, and a certificate exchange that is the one
              new public route in Phase 1. It is driven from the agent, not the browser.
            </p>
            <p>
              <code>features/pairing/AgentPairing.tsx</code> is the sharpest example of why this
              page is blank instead of populated. It displays a fixed{" "}
              <code>SPIFFE Trust Domain: spiffe://cluster.local</code> and the status{" "}
              <em>Connected &amp; Attested</em>, with no props and no fetch — asserting a verified,
              attested agent connection that nothing had checked. A security control reported as
              passing by a component that cannot observe it is the most damaging kind of
              placeholder, so it is not mounted.
            </p>
          </div>
        }
      />
    </div>
  );
}
