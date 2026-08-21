// SPDX-License-Identifier: FSL-1.1-ALv2
import { ApprovalCenter } from "@/features/approvals/ApprovalCenter";

export default function ApprovalsPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Approvals</h1>
        <p className="mt-1 text-muted-foreground">
          Change sets awaiting a decision, read from <code>GET /api/v1/approvals</code>. Approving
          or rejecting one goes through the governance chokepoint, so the decision is recorded
          against your authenticated identity and written to the audit log in the same transaction.
        </p>
      </div>
      <ApprovalCenter />
    </div>
  );
}
