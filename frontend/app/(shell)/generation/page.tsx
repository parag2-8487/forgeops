// SPDX-License-Identifier: FSL-1.1-ALv2
import { NotImplemented } from "@/components/ui/not-implemented";

export default function GenerationPage() {
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold tracking-tight">Generation</h1>
      <NotImplemented
        feature="The artifact generator"
        owner="Phase 1 for the endpoint, which now exists; wiring the wizard to it is the remaining work"
        reason="POST /api/v1/generation/runs streams a real run, but GeneratorWizard has never been connected to it and its dropdown options are still hardcoded."
        detail={
          <div className="space-y-2 text-muted-foreground">
            <p className="font-medium text-foreground">What changed, and what is genuinely left.</p>
            <p>
              This panel used to say <code>generation/</code> had{" "}
              <em>no routes.py, so the pipeline has no HTTP surface</em>. That is no longer true:{" "}
              <code>POST /api/v1/generation/runs</code> is mounted and authenticated, streams the
              six-event SSE vocabulary, records a <code>generation_runs</code> row, and submits the
              artifacts it produced to the governance chokepoint as a change set.
            </p>
            <p>
              What is missing is the browser end.{" "}
              <code>features/generation/GeneratorWizard.tsx</code> is a three-step form whose final
              step reads &ldquo;Ready to generate artifacts&rdquo; and still has no submit handler,
              and its options are hardcoded rather than read from the project. Mounting it now would
              put a working-looking button in front of an endpoint it does not call, which is the
              defect this panel exists to avoid.
            </p>
          </div>
        }
      />
    </div>
  );
}
