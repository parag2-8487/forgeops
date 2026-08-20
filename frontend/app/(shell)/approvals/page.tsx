// SPDX-License-Identifier: FSL-1.1-ALv2
import { NotImplemented } from "@/components/ui/not-implemented";

export default function ApprovalsPage() {
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold tracking-tight">Approvals</h1>
      <NotImplemented
        feature="The Change Approval Center"
        owner="Phase 1 for the endpoint, which now exists; the reviewable diff UI is the remaining work"
        reason="The backend surface is mounted and authenticated, but no screen has been built to render a change-set diff or submit a decision."
        detail={
          <div className="space-y-2 text-muted-foreground">
            <p className="font-medium text-foreground">What changed, and what is genuinely left.</p>
            <p>
              This panel used to say the router was <em>not registered</em> and{" "}
              <em>required no authentication</em>. Both statements were true and are now false.{" "}
              <code>/api/v1/approvals</code> is mounted with router-level{" "}
              <code>require_principal</code>, the approver comes from the verified principal rather
              than a query parameter defaulting to <code>admin</code>, and every transition goes
              through the governance chokepoint over the real <code>change_sets</code> table.
            </p>
            <p>
              What is missing is this screen. Five routes are available — list, read one with its{" "}
              <code>change_items</code>, approve, reject and revert — and rendering a diff with the
              two view modes the design calls for, plus a decision form carrying a comment and the
              displayed version for optimistic concurrency, is a real piece of UI work rather than a
              wiring gap. Until it exists this page says so instead of showing a diff of nothing.
            </p>
          </div>
        }
      />
    </div>
  );
}
