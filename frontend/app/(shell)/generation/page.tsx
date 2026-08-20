// SPDX-License-Identifier: FSL-1.1-ALv2
import { NotImplemented } from "@/components/ui/not-implemented";

export default function GenerationPage() {
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold tracking-tight">Generation</h1>
      <NotImplemented
        feature="The artifact generator"
        owner="Phase 1 deliverable 1.5 for the pipeline; an interactive generator UI is Phase 2 work"
        reason="backend/src/generation/ has a service, schemas and models but no routes.py, so the pipeline has no HTTP surface for a browser to call."
        detail={
          <div className="space-y-2 text-muted-foreground">
            <p>
              The generation pipeline itself is real and covered — dry-run, validation and the
              feedback loop all exist as Python modules, and the end-to-end journey exercises them.
              What is missing is the request surface: nothing maps an HTTP call onto{" "}
              <code>generation/service.py</code>.
            </p>
            <p>
              <code>features/generation/GeneratorWizard.tsx</code> is a three-step form whose final
              step reads &ldquo;Ready to generate artifacts&rdquo; and has no submit handler. Its
              dropdown options are hardcoded. Rendering it here would suggest a working generator
              behind a button that does nothing, so it is left unmounted until there is an endpoint
              to submit to.
            </p>
          </div>
        }
      />
    </div>
  );
}
