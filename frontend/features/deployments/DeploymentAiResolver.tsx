// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useCallback, useState } from "react";
import { api, ApiProblemError } from "@/lib/api";
import { isSseEvent, isTerminalSseEvent, type SseEvent } from "@/lib/api/sse-events";
import { readSSEResponse } from "@/lib/sse-reader";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Sparkles, RefreshCw, CheckCircle2, AlertTriangle, ExternalLink, Loader2, Wrench, Terminal } from "lucide-react";
import Link from "next/link";

interface DeploymentAiResolverProps {
  projectId: string;
  deploymentId: string;
  manifests: string[];
  errorMessage: string;
  onRetryDeploy?: () => Promise<void> | void;
}

type ResolverState = "idle" | "streaming" | "accepted" | "failed";

export function DeploymentAiResolver({
  projectId,
  deploymentId,
  manifests,
  errorMessage,
  onRetryDeploy,
}: DeploymentAiResolverProps) {
  const [isOpen, setIsOpen] = useState(false);
  const [state, setState] = useState<ResolverState>("idle");
  const [liveOutput, setLiveOutput] = useState("");
  const [generatedFiles, setGeneratedFiles] = useState<string[]>([]);
  const [changeSetId, setChangeSetId] = useState<string | null>(null);
  const [failureReason, setFailureReason] = useState<string | null>(null);
  const [isApproving, setIsApproving] = useState(false);
  const [isApproved, setIsApproved] = useState(false);
  const [isRedeploying, setIsRedeploying] = useState(false);
  const [redeployError, setRedeployError] = useState<string | null>(null);

  const resolveWithAi = useCallback(async () => {
    setIsOpen(true);
    setState("streaming");
    setLiveOutput("");
    setGeneratedFiles([]);
    setChangeSetId(null);
    setFailureReason(null);
    setIsApproved(false);

    let sawTerminal = false;

    // High-context repair prompt: classifies the error, feeds the AI the full diagnostic context,
    // and instructs it to fix the GENERATED artifacts (Dockerfile, compose, k8s manifests), not the
    // user's application source code.
    const errorLower = errorMessage.toLowerCase();

    // Classify the error into actionable categories so the AI receives structured diagnosis.
    //
    // These signatures are the ones a real toolchain emits, taken from actual build logs rather than
    // from docker's own wording: a TypeScript failure reads `error TS7016: Could not find a
    // declaration file for module 'react'`, which contains neither "failed" nor "exit code" and would
    // otherwise reach the model unclassified.
    const errorCategories: string[] = [];

    // --- build stage never ran the compiler ------------------------------------------------
    if (errorLower.includes("exit code: 127") || errorLower.includes("exit code 127") || errorLower.includes("command not found")) {
      errorCategories.push("BUILD_TOOL_MISSING: A command was not found inside the builder stage. For Node this is almost always `npm install --production` / `--omit=dev` stripping devDependencies, where the build CLIs (tsc, vite, webpack, esbuild, next) live. Fix: install WITHOUT --production in the builder stage. For a missing interpreter (node, python, go, mvn, cargo), fix the base image of the builder stage instead.");
    }

    // --- language-level compile failures ---------------------------------------------------
    if (/error ts\d+/.test(errorLower) || errorLower.includes("cannot find type definition file") || errorLower.includes("implicitly has an 'any' type")) {
      errorCategories.push("TYPESCRIPT_COMPILE_ERROR: `tsc` rejected the sources. Distinguish two causes. (a) The application's own code or tsconfig is wrong -- report that, do NOT invent files to silence it. (b) The Dockerfile installed production-only dependencies, so @types/* packages are absent and every import resolves to `any`. For (b), install devDependencies in the builder stage.");
    }
    if (errorLower.includes("module not found") || errorLower.includes("cannot find module") || errorLower.includes("could not resolve")) {
      errorCategories.push("MISSING_DEPENDENCY: A module could not be resolved at build or start time. Check that the dependency is declared in the manifest AND that the Dockerfile installs it (a production-only install omits devDependencies). If the module is genuinely undeclared in the application, report that instead of adding it.");
    }
    if (errorLower.includes("err_pnpm") || errorLower.includes("err!") || errorLower.includes("npm err")) {
      errorCategories.push("PACKAGE_MANAGER_ERROR: The package manager itself failed. Match the install command to the lockfile actually present (package-lock.json -> npm ci/npm install, yarn.lock -> yarn, pnpm-lock.yaml -> pnpm, bun.lock -> bun). Never run an install command whose lockfile is absent.");
    }
    if (errorLower.includes("no such file or directory") || errorLower.includes("not found") || errorLower.includes("failed to compute cache key") || errorLower.includes("checksum")) {
      errorCategories.push("DOCKER_COPY_PATH_MISSING: A COPY instruction references a path that does not exist in the build context. Linux builds are case-sensitive, so every COPY, WORKDIR and build context must match the exact casing and nesting of files on disk. Copy from the directory that actually contains the file; do not add placeholder files to satisfy a COPY.");
    }
    if (errorLower.includes("failed to solve") || errorLower.includes("dockerfile parse error") || errorLower.includes("unknown type")) {
      errorCategories.push("DOCKERFILE_SYNTAX_ERROR: The Dockerfile has a syntax error (malformed HEALTHCHECK, stray backslash, invalid instruction, wrong stage name in COPY --from).");
    }
    if (errorLower.includes("server.js") && (errorLower.includes("not found") || errorLower.includes("cannot find module"))) {
      errorCategories.push("WRONG_ENTRYPOINT: CMD references a file that does not exist. A static frontend (Vite, React, Vue, Angular, Svelte) has no server.js: serve the built directory with nginx or `serve -s` instead of `node server.js`.");
    }
    if (errorLower.includes("version") && (errorLower.includes("unsupported") || errorLower.includes("requires node") || errorLower.includes("engine"))) {
      errorCategories.push("RUNTIME_VERSION_MISMATCH: The declared or lockfile-pinned runtime version is incompatible with the base image. Align the base image tag with what the project actually requires (engines field, .nvmrc, go.mod, pom.xml) instead of loosening the project's requirement.");
    }

    // --- infrastructure rather than project -------------------------------------------------
    if (errorLower.includes("timeout") || errorLower.includes("tls handshake") || errorLower.includes("failed to resolve reference") || errorLower.includes("failed to do request")) {
      errorCategories.push("NETWORK_TIMEOUT: An image pull or registry request timed out. This is transient and environmental -- it is NOT a defect in the Dockerfile. Report it as such and retry rather than rewriting the build.");
    }
    if (errorLower.includes("permission denied") || errorLower.includes("operation not permitted")) {
      errorCategories.push("PERMISSION_ERROR: A file operation was denied. Usually a COPY --from whose source path is wrong, or a USER that cannot read the copied files. Prefer fixing the path or the ownership over removing the USER directive, which exists for a reason.");
    }
    if (errorLower.includes("port is already allocated") || errorLower.includes("address already in use") || errorLower.includes("conflict")) {
      errorCategories.push("PORT_CONFLICT: A host port is already taken. Change the published host port; keep the container port aligned with what the application listens on.");
    }
    if (errorLower.includes("no space left") || errorLower.includes("out of memory") || errorLower.includes("killed")) {
      errorCategories.push("RESOURCE_EXHAUSTION: The host ran out of disk or memory during the build. Report it as environmental; do not rewrite the application to work around it.");
    }

    const diagnosisBlock = errorCategories.length > 0
      ? `\n\nDIAGNOSIS (auto-classified from the error):\n${errorCategories.map((c, i: number) => `  ${i + 1}. ${c}`).join("\n")}`
      : "";

    // BOUND THE ERROR EXCERPT BEFORE THE REQUEST.
    //
    // The whole server response is sent, and a failing build can produce thousands of lines. The
    // endpoint rejects an over-long prompt with a 422 whose message is "One or more fields failed
    // validation" — which says nothing about the prompt, the length, or the field, so a user sees a
    // health/schema-sounding error for what is really "your log was too big". Trimming here keeps a
    // repair request inside the limit by construction, and the trim keeps the TAIL, because every
    // toolchain states the cause in its last lines.
    const MAX_ERROR_CHARS = 9000;
    const errorExcerpt =
      errorMessage.length > MAX_ERROR_CHARS
        ? `[... ${errorMessage.length - MAX_ERROR_CHARS} earlier characters omitted ...]\n` +
          errorMessage.slice(-MAX_ERROR_CHARS)
        : errorMessage;

    const prompt = `DEPLOYMENT FAILURE REPAIR REQUEST

FULL ERROR OUTPUT FROM THE HOST (this is the real build log, not a summary):
${errorExcerpt}

ATTEMPTED MANIFEST(S): ${manifests.join(", ")}
${diagnosisBlock}

HOW TO FIND THE ROOT CAUSE
1. Read the error output above and identify the FIRST line that reports a real problem -- everything
   after it is usually a consequence. Quote that line in your reasoning.
2. Decide which layer failed: dependency install, compile/build of the application, container
   packaging (Dockerfile), or the runtime entrypoint. Each has a different correct fix.
3. Inspect this project's actual files and directory layout from the repository index. Do not assume
   a layout: this project may put its application at the repository root, in a subdirectory such as
   frontend/ or apps/web/, or across several workspace packages.
4. Determine whether the fault is in the GENERATED deployment artifacts or in the application itself.

HOW TO FIX
- If the fault is in the artifacts (Dockerfile, compose, Kubernetes manifests, build context, install
  or build command, entrypoint, exposed port, working directory), correct them. This is the usual case.
- In a BUILDER stage, never install production-only. Build CLIs and @types/* packages live in
  devDependencies, and stripping them is the single most common cause of a failed build. Run the build
  with the devDependencies present, then install production-only in the final stage if you want a
  smaller image.
- Match the package manager to the lockfile that is actually present. Never run 'npm ci' without a
  package-lock.json, and never run a build script the manifest does not declare.
- Match the base image to the runtime the project requires. Do not downgrade the project to fit the image.
- Every COPY/WORKDIR/context path must match the exact case and nesting on disk. Linux is case-sensitive.
- A static frontend has no server.js. Serve its build output (nginx, or 'serve -s <dir>') rather than
  starting a Node process that does not exist.
- Keep the container port, the EXPOSE instruction, the application's listen port, and the compose or
  Service port consistent with each other.
- Preserve any valid configuration the project already carries. Repair it; do not replace it wholesale.

WHAT NOT TO DO
- Do NOT modify the application's source code, package.json, lockfiles, or build scripts. Those belong
  to the user. Fix the deployment artifacts around them.
- Do NOT invent files, services, manifests, or dependencies to make an error disappear. If the
  application itself is broken -- invalid source, a missing secret, an unavailable private dependency,
  a genuinely broken build script, an unsupported OS dependency -- say so plainly, name the exact file
  and line, and explain what the user must change. A clear explanation is a better outcome than a
  speculative file that hides the real fault.
- Do NOT create a workaround specific to this one application. Whatever you change must hold for any
  project with the same characteristics.

OUTPUT
Produce the corrected Dockerfile, docker-compose.yml, and Kubernetes manifests for this project.`;

    try {
      const response = await api.stream("/generation/runs", {
        method: "POST",
        body: JSON.stringify({ project_id: projectId, prompt, environment: null }),
      });

      let live = "";
      for await (const message of readSSEResponse<any>(response)) {
        if (!isSseEvent(message.event)) continue;
        const event = message.event as SseEvent;

        if (event === "token") {
          if (message.data?.text) {
            live += message.data.text;
            setLiveOutput(live);
          }
        } else if (event === "complete") {
          const payload = message.data;
          if (payload?.files) {
            setGeneratedFiles(payload.files);
          }
          if (payload?.change_set_id) {
            setChangeSetId(payload.change_set_id);
          }
          setState("accepted");
        } else if (event === "error") {
          setFailureReason(message.data?.detail ?? "AI repair run failed.");
          setState("failed");
        }

        if (isTerminalSseEvent(event)) {
          sawTerminal = true;
          break;
        }
      }

      if (!sawTerminal) {
        setState("failed");
        setFailureReason("The generation stream closed without a terminal event.");
      }
    } catch (err: unknown) {
      const problem = err instanceof ApiProblemError ? err.problem : null;
      setFailureReason(problem?.detail ?? problem?.title ?? "Failed to request AI generation.");
      setState("failed");
    }
  }, [projectId, manifests, errorMessage]);

  const approveFix = useCallback(async () => {
    if (!changeSetId) return;
    setIsApproving(true);
    try {
      await api.post(`/approvals/${changeSetId}/approve`, {
        comment: "Auto-approved fix from deployment error resolution",
        expected_version: 1,
      });
      setIsApproved(true);
    } catch {
      // If version mismatch or already approved, mark as approved
      setIsApproved(true);
    } finally {
      setIsApproving(false);
    }
  }, [changeSetId]);

  const handleRedeploy = useCallback(async () => {
    if (!onRetryDeploy || isRedeploying) return;
    setIsRedeploying(true);
    setRedeployError(null);
    try {
      if (changeSetId && !isApproved) {
        await approveFix();
      }
      await onRetryDeploy();
    } catch (err: unknown) {
      const problem = err instanceof ApiProblemError ? err.problem : null;
      setRedeployError(problem?.detail ?? (err instanceof Error ? err.message : "Redeploy failed."));
    } finally {
      setIsRedeploying(false);
    }
  }, [onRetryDeploy, isRedeploying, changeSetId, isApproved, approveFix]);

  return (
    <div className="space-y-3 pt-2">
      {/* Action buttons row */}
      <div className="flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          variant="default"
          className="gap-1.5 text-xs bg-primary hover:bg-primary/90 text-primary-foreground font-medium shadow-xs"
          onClick={resolveWithAi}
          disabled={state === "streaming" || isRedeploying}
        >
          {state === "streaming" ? (
            <>
              <Loader2 className="size-3.5 animate-spin" />
              <span>AI Resolving Errors…</span>
            </>
          ) : (
            <>
              <Sparkles className="size-3.5" />
              <span>Resolve Errors with AI</span>
            </>
          )}
        </Button>

        {onRetryDeploy && (
          <Button
            size="sm"
            variant="outline"
            className="gap-1.5 text-xs"
            onClick={handleRedeploy}
            disabled={state === "streaming" || isRedeploying}
          >
            {isRedeploying ? (
              <Loader2 className="size-3.5 animate-spin" />
            ) : (
              <RefreshCw className="size-3.5" />
            )}
            <span>{isRedeploying ? "Redeploying…" : "Retry Deploy"}</span>
          </Button>
        )}
      </div>

      {/* Expanded AI Resolution Panel */}
      {isOpen && (
        <div
          data-testid="ai-resolution-panel"
          className="rounded-lg border border-primary/30 bg-card/95 p-4 shadow-sm space-y-3 transition-all"
        >
          <div className="flex items-center justify-between border-b border-border/70 pb-2.5">
            <div className="flex items-center gap-2">
              <Wrench className="size-4 text-primary" />
              <h4 className="text-xs font-semibold text-foreground">
                AI Automated Remediation
              </h4>
            </div>
            <div className="flex items-center gap-2">
              {state === "streaming" && (
                <Badge variant="outline" className="gap-1 text-[11px] border-primary/50 text-primary animate-pulse">
                  <Loader2 className="size-3 animate-spin" />
                  Model Generating Fixes…
                </Badge>
              )}
              {state === "accepted" && (
                <Badge variant="success" className="gap-1 text-[11px]">
                  <CheckCircle2 className="size-3" />
                  Fix Ready
                </Badge>
              )}
              {state === "failed" && (
                <Badge variant="destructive" className="gap-1 text-[11px]">
                  <AlertTriangle className="size-3" />
                  Resolution Failed
                </Badge>
              )}
            </div>
          </div>

          {/* Stepper info */}
          <div className="text-xs text-muted-foreground space-y-1">
            <p>
              <span className="font-semibold text-foreground">Diagnosis:</span> The model is analyzing host build logs and adapting configuration to this project&apos;s runtime.
            </p>
          </div>

          {/* Live streaming output while working */}
          {state === "streaming" && (
            <div className="space-y-1.5">
              <div className="flex items-center gap-1.5 text-[11px] font-mono text-muted-foreground">
                <Terminal className="size-3" />
                <span>Live Model Stream</span>
              </div>
              <pre className="max-h-52 overflow-y-auto whitespace-pre-wrap rounded-md border border-zinc-800 bg-zinc-950 p-3 font-mono text-[11px] text-zinc-200">
                {liveOutput || "Receiving streaming fixes from generation model…"}
              </pre>
            </div>
          )}

          {/* Accepted state */}
          {state === "accepted" && (
            <div className="space-y-3 rounded-md border border-emerald-500/20 bg-emerald-500/5 p-3">
              <div className="flex items-center gap-2 text-xs font-medium text-emerald-400">
                <CheckCircle2 className="size-4 shrink-0" />
                <span>
                  Corrected configuration generated ({generatedFiles.length} file(s):{" "}
                  {generatedFiles.join(", ")})
                </span>
              </div>
              <p className="text-xs text-muted-foreground">
                The AI corrected the Dockerfile and deployment manifests. You can apply the change set directly and re-run deployment.
              </p>

              <div className="flex flex-wrap items-center gap-2 pt-1">
                {!isApproved ? (
                  <Button
                    size="sm"
                    className="gap-1.5 text-xs bg-emerald-600 hover:bg-emerald-700 text-white"
                    onClick={approveFix}
                    disabled={isApproving}
                  >
                    {isApproving ? (
                      <>
                        <Loader2 className="size-3.5 animate-spin" />
                        <span>Applying to Disk…</span>
                      </>
                    ) : (
                      <>
                        <CheckCircle2 className="size-3.5" />
                        <span>Approve & Apply to Disk</span>
                      </>
                    )}
                  </Button>
                ) : (
                  <Badge variant="success" className="gap-1 py-1 text-xs">
                    <CheckCircle2 className="size-3.5" />
                    Applied to Disk!
                  </Badge>
                )}

                {onRetryDeploy && (
                  <Button
                    size="sm"
                    variant={isApproved ? "default" : "outline"}
                    className="gap-1.5 text-xs"
                    onClick={handleRedeploy}
                    disabled={isRedeploying}
                  >
                    {isRedeploying ? (
                      <Loader2 className="size-3.5 animate-spin" />
                    ) : (
                      <RefreshCw className="size-3.5" />
                    )}
                    <span>{isRedeploying ? "Redeploying…" : "Re-Deploy Now"}</span>
                  </Button>
                )}

                <Link
                  href="/approvals"
                  className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground underline underline-offset-4 ml-1"
                >
                  <span>Review in Approvals</span>
                  <ExternalLink className="size-3" />
                </Link>
              </div>

              {redeployError && (
                <p className="text-xs text-destructive font-medium pt-1">
                  {redeployError}
                </p>
              )}
            </div>
          )}

          {/* Failed state */}
          {state === "failed" && (
            <div className="space-y-2 rounded-md border border-destructive/30 bg-destructive/10 p-3">
              <p className="text-xs text-destructive font-medium">
                {failureReason || "Could not generate automated fix for this error."}
              </p>
              <Button size="sm" variant="outline" className="text-xs" onClick={resolveWithAi}>
                Try Again
              </Button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
