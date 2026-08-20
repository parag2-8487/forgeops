// SPDX-License-Identifier: FSL-1.1-ALv2
import { NotImplemented } from "@/components/ui/not-implemented";

export default function ApprovalsPage() {
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold tracking-tight">Approvals</h1>
      <NotImplemented
        feature="The Change Approval Center"
        owner="Phase 1 deliverable 1.6 for the backend mechanism; the reviewable UI is not scheduled until the approval surface is safe to mount"
        reason="The backend router exists and is not registered, so there is no endpoint to call. Mounting it as it stands would be a security regression, not a quick win."
        detail={
          <div className="space-y-2 text-muted-foreground">
            <p className="font-medium text-foreground">
              Why an existing router is deliberately left unmounted.
            </p>
            <p>
              <code>backend/src/approvals/routes.py</code> implements list, get, approve, reject and
              rollback, and has tests. It is absent from the twelve routers <code>create_app</code>{" "}
              registers, and it should stay absent until two defects are fixed.
            </p>
            <p>
              <strong>It requires no authentication.</strong> Neither the router nor any route
              depends on <code>require_principal</code>, so mounting it would expose change-set
              approval to anonymous callers. Worse, <code>approve</code> takes the approver as a{" "}
              <em>query parameter</em> defaulting to <code>admin</code> — a caller-supplied
              identity, which contradicts the rule the rest of the system is built on: a principal
              is constructed only by a verifier, never from request data. The approval gate is the
              one control that must not be bypassable.
            </p>
            <p>
              <strong>Its store is a dictionary.</strong> <code>ApprovalService</code> holds
              change-sets in an in-process dict, so state would be lost on restart and would differ
              between workers, despite the module describing them as persisted.
            </p>
            <p>
              <code>scripts/check-route-auth.py</code>, which CI runs, asserts every route either
              depends on <code>require_principal</code> or is listed public. It would fail the build
              if this router were mounted — which is the gate doing its job.
            </p>
          </div>
        }
      />
    </div>
  );
}
