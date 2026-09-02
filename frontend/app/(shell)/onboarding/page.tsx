// SPDX-License-Identifier: FSL-1.1-ALv2
"use client";

import { useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { ProjectPicker } from "@/components/ui/project-picker";
import type { CodebaseStatus, ProjectPage } from "@/features/projects/types";
import { AgentConnectPanel } from "@/features/agent/AgentConnectPanel";

/**
 * Nothing to an applied change set, in order, with the current state of each step observed.
 *
 * WHY THIS SCREEN EXISTS AT ALL
 * Every capability on the path was reachable and none of them said what had to come first, so a new
 * user could not get from an empty database to a generated artifact through the browser. The SSE paint
 * test found out why the hard way: a freshly created project has no published policy bundle and no
 * paired device, so the governance chokepoint refuses its change-set submission even though generation
 * and validation both succeeded. From the user's side that is a `policy-bundle-stale` error at the end
 * of a long run, four layers from its cause, on a screen that never mentioned bundles.
 *
 * The ordering is not a suggestion. Each step is a precondition of the next:
 *
 *   1. create a project      — everything else is scoped to one
 *   2. mint a pairing code   — scoped to a project, so (1) first
 *   3. pair the agent        — consumes the code, single use, five minutes
 *   4. scan                  — the agent needs its credential from (3) to submit an index
 *   5. publish the bundle    — the chokepoint refuses submissions from an unpinned device
 *   6. generate              — needs the index from (4) for context
 *   7. approve               — the gate holds what (6) submitted
 *   8. apply                 — the agent from (3) writes the files
 *
 * WHAT IS OBSERVED AND WHAT IS NOT, STATED RATHER THAN IMPLIED
 * Steps 1, 2/3 and 4 are checked against real endpoints: the project list, the device list, and the
 * codebase index status. Step 5 is NOT: there is no read route for "is a bundle published for this
 * tenant", so this screen does not claim to know. It says so, and gives the control, rather than
 * showing a tick it cannot justify — which is the whole failure mode this pass exists to remove.
 * Steps 6 to 8 are actions rather than states and are linked, not asserted.
 */

interface DevicePage {
  devices: {
    id: string;
    project_id: string;
    status: string;
    heartbeat_fresh: boolean | null;
  }[];
}

/**
 * What this screen can say about a step. The cases are deliberately distinct.
 *
 * WHY "Not checked" WAS REPLACED. One label covered three unrelated situations — a check still in
 * flight, a step that is an ACTION with no resting state, and a step nothing here has a read route
 * for — and it read as "not working" in all of them. That is worse than saying nothing: a user who
 * has done everything right sees four grey labels and concludes the product is broken. The
 * discipline behind it was right (never a tick for something nothing checked) and is kept; only the
 * wording that collapsed three meanings into one is gone.
 *
 *   done      a real check answered yes
 *   todo      a real check answered no
 *   checking  a real check is in flight
 *   action    something you DO; there is no resting state that would mean "done"
 *   no-route  observable in principle, but this screen has no endpoint that reports it
 */
type StepState = "done" | "todo" | "checking" | "action" | "no-route";

export default function OnboardingPage() {
  const [projectId, setProjectId] = useState("");

  const projects = useQuery({
    queryKey: queryKeys.projects.list(100),
    queryFn: () => api.get<ProjectPage>("/projects?limit=100"),
    retry: false,
  });

  const devices = useQuery({
    queryKey: queryKeys.devices.list(),
    queryFn: () => api.get<DevicePage>("/agents/devices?limit=100"),
    retry: false,
  });

  const index = useQuery({
    queryKey: queryKeys.codebase.status(projectId),
    queryFn: () => api.get<CodebaseStatus>(`/analysis/codebase/${projectId}/status`),
    enabled: projectId !== "",
    retry: false,
  });

  // The endpoint applies the SAME predicate the chokepoint does, including an installation-wide
  // bundle standing in for a project that has none of its own — so this screen and the refusal a
  // user would otherwise hit cannot disagree about whether anything is published.
  const bundle = useQuery({
    queryKey: ["policies", "active-bundle", projectId],
    queryFn: () =>
      api.get<{ digest: string | null; published_at: string | null }>(
        projectId === ""
          ? "/policies/active-bundle"
          : `/policies/active-bundle?project_id=${projectId}`,
      ),
    retry: false,
  });
  const activeDigest = bundle.data?.digest ?? null;

  const hasProject = (projects.data?.projects.length ?? 0) > 0;
  const devicesForProject =
    projectId === "" ? [] : (devices.data?.devices ?? []).filter((d) => d.project_id === projectId);
  const pendingDevice = devicesForProject.some((d) => d.status === "pending");
  const activeDevice = devicesForProject.some((d) => d.status === "active");
  const indexed = index.data ? index.data.indexed_files > 0 : false;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Getting started</h1>
        <p className="mt-1 text-muted-foreground">
          Eight steps from an empty installation to an applied change. Each one is a precondition of
          the next, and skipping one shows up several steps later as an error that does not name it
          — which is why they are in order rather than being a list of features.
        </p>
      </div>

      <div className="rounded-lg border border-border bg-background p-4">
        <p className="text-sm font-medium">Which project are you setting up?</p>
        <p className="mt-1 text-xs text-muted-foreground">
          Steps 2 to 8 are all scoped to one project, so the state below is for the one chosen here.
        </p>
        <div className="mt-3">
          {hasProject ? (
            <ProjectPicker value={projectId} onChange={setProjectId} id="onboarding-project" />
          ) : (
            <p className="text-sm text-muted-foreground">
              None exists yet — step 1 is what creates one.
            </p>
          )}
        </div>
      </div>

      <ol className="space-y-4" data-testid="onboarding-steps">
        <Step
          n={1}
          title="Create a project"
          state={hasProject ? "done" : "todo"}
          observedBy="GET /api/v1/projects"
          done="At least one project exists in this tenant."
          todo="Nothing else can be scoped without one. A project records the name, the repository and the working-tree path an agent will later scan."
          href="/projects"
          hrefLabel="Create a project"
        />

        <Step
          n={2}
          title="Mint a pairing code"
          state={projectId === "" ? "todo" : pendingDevice || activeDevice ? "done" : "todo"}
          observedBy="GET /api/v1/agents/devices"
          done={
            activeDevice
              ? "A device is already paired to this project, so a new code is only needed for an additional machine."
              : "A code has been minted and its device row is pending exchange."
          }
          todo="A code pairs one machine to this project and expires in five minutes. It is shown once and stored nowhere, so mint it when you are at the machine that will use it."
          href="/pairing"
          hrefLabel="Mint a code"
        />

        <Step
          n={3}
          title="Pair the agent"
          state={projectId === "" ? "todo" : activeDevice ? "done" : "todo"}
          observedBy="GET /api/v1/agents/devices"
          done="A device is active for this project, with a certificate issued by the internal CA."
          todo="Download the agent, put it on your PATH, and run one command. The panel below prints all three, correct for your platform — no editing, and nothing to compile."
          href="/pairing"
          hrefLabel="See device state"
        />

        {/* The commands themselves, on this page, correct for the reader's platform.
         *
         * The step above used to say: run `forgeops-agent pair --code <code>`. That command does not
         * work. On Windows PowerShell will not execute a bare name from the current directory and the
         * binary is `forgeops-agent.exe`; everywhere, `pair` refuses without `--backend`, and the
         * value was derivable only from BACKEND_PORT in the repository's .env. Three corrections a
         * user had to make before anything happened, none of them written down.
         *
         * Rendered here rather than only on /pairing so this page alone takes a user from nothing to
         * a paired, scanning agent — which is what `onboarding.spec.ts` now follows literally. */}
        {/* The commands themselves live BELOW this list, outside it.
         *
         * Not inside, and the reason is mechanical: the panel renders its own three numbered steps as
         * `<li>` elements, and `onboarding.spec.ts` asserts the count of `li` under
         * `onboarding-steps` is exactly eight. Nesting it here made that eleven. A list whose length
         * is an assertion is a list that must contain only its own items. */}
        <Step
          n={4}
          title="Scan the codebase"
          state={
            projectId === "" ? "todo" : index.isPending ? "checking" : indexed ? "done" : "todo"
          }
          observedBy="GET /api/v1/analysis/codebase/{id}/status"
          done={
            index.data
              ? `${index.data.indexed_files} file(s) and ${index.data.total_chunks} chunk(s) are indexed.`
              : "The index is populated."
          }
          todo="Until this runs, readiness cannot be scored, retrieval has nothing to search, and generation runs without context from your code. There is no button: the backend cannot tell an agent to scan (§2.2.1 confines command dispatch to the governance chokepoint), so the exact command is on the project's own page."
          href={projectId === "" ? "/projects" : `/projects/${projectId}`}
          hrefLabel="Get the scan command"
        />

        <Step
          n={5}
          title="Publish the policy bundle"
          // NOW CHECKED, rather than disclaimed. This was permanently unreportable for want of a read
          // route, and it is the step easiest to skip and hardest to diagnose — so it is the one worth
          // an endpoint. `GET /policies/active-bundle` is the one-row query the chokepoint was already
          // making on every submission: the digest existed and nothing exposed it.
          state={bundle.isPending ? "checking" : activeDigest ? "done" : "todo"}
          observedBy="GET /api/v1/policies/active-bundle"
          done={
            activeDigest
              ? `Active bundle ${activeDigest.slice(0, 19)}… — a device paired now is pinned to this digest.`
              : ""
          }
          todo="THE STEP THAT IS EASIEST TO SKIP AND HARDEST TO DIAGNOSE. The chokepoint refuses a change-set submission from any agent not pinned to the tenant's current bundle digest, so an unpublished tenant fails at step 7 with a stale-bundle error that says nothing about bundles. Publish once now, and again after every policy change you want enforced."
          href="/policies"
          hrefLabel="Publish the bundle"
        />

        <Step
          n={6}
          title="Generate an artifact"
          state="action"
          observedBy={null}
          done=""
          todo="Streams a real run over the six §7.4 event types. Artifacts that pass the deterministic validation gate are submitted to the chokepoint as a change set rather than written anywhere directly — so a successful generation ends with something to review, not with files on disk."
          href="/generation"
          hrefLabel="Open the generator"
          unknownNote="Nothing here to tick: a generation is something you run, not a state to rest in. What a run produced appears in the project's change history."
        />

        <Step
          n={7}
          title="Approve the change set"
          state="action"
          observedBy={null}
          done=""
          todo="The gate holds what step 6 submitted. Approving mints authority and hands the agent a signed command; rejecting writes the refusal to the audit chain. Either way the decision is attributed to your authenticated identity — there is no field for the approver."
          href="/approvals"
          hrefLabel="Open the approval centre"
          unknownNote="Nothing here to tick: approving is a decision you make. The Awaiting decision queue stays empty until step 6 produces something to decide on."
        />

        <Step
          n={8}
          title="The agent applies it"
          state="no-route"
          observedBy={null}
          done=""
          todo="Nothing to do here — the agent paired in step 3 receives the signed command and writes the files, taking a timestamped backup first. If the write fails partway it restores from that backup and reports `rolled_back`, which is a different state from a deliberate `reverted`."
          href={projectId === "" ? "/approvals" : `/projects/${projectId}`}
          hrefLabel="Watch the change history"
          unknownNote="Reported by the project's change history rather than by this screen, which has no read route for the queue and does not guess. A change set moves applying → applied when the agent finishes, → rolled_back when it had to undo a partial write, and → reverted only when somebody deliberately reverses an applied one."
        />
      </ol>

      {/* Every command a first run needs, correct for the reader's platform, with nothing to edit.
       *
       * Step 3 above used to say: run `forgeops-agent pair --code <code>`. That command does not
       * work. On Windows PowerShell will not execute a bare name from the current directory and the
       * binary is `forgeops-agent.exe`; everywhere, `pair` refuses without `--backend`, and the value
       * was derivable only from BACKEND_PORT in the repository's own `.env`. Three corrections a user
       * had to make before anything happened, none of them written down anywhere.
       *
       * Rendered on THIS page rather than only on /pairing, so this screen alone takes a user from
       * nothing to a paired, scanning agent — which is what `printed-instructions.spec.ts` follows
       * literally, executing what it reads out of the DOM. */}
      {projectId === "" ? null : <AgentConnectPanel projectId={projectId} />}
      <aside className="rounded-lg border border-border bg-muted/30 p-4 text-xs text-muted-foreground">
        <p className="font-medium text-foreground">Why a missing step is reported as a step.</p>
        <p className="mt-2">
          Four of the eight steps above have a real read route behind them, and this screen checks
          those and only those. The other four say so. That matters more than it looks: the failure
          this page exists to prevent is a user reaching step 7, being told{" "}
          <code>policy-bundle-stale</code>, and having no way to learn that the missing thing was
          step 5. Every governance refusal in the app now carries the same treatment — what the rule
          is for, and which step to go and do — rather than the registry&apos;s own wording alone.
        </p>
      </aside>
    </div>
  );
}

function Step({
  n,
  title,
  state,
  observedBy,
  done,
  todo,
  href,
  hrefLabel,
  unknownNote,
}: {
  n: number;
  title: string;
  state: StepState;
  /** The endpoint whose answer decided `state`, or `null` when nothing can report it. */
  observedBy: string | null;
  done: string;
  todo: string;
  href: string;
  hrefLabel: string;
  unknownNote?: string;
}) {
  const badge = {
    done: "Done",
    todo: "Not yet",
    checking: "Checking…",
    action: "Your move",
    "no-route": "Not reported here",
  }[state];

  return (
    <li
      className="rounded-lg border border-border bg-background p-4"
      data-testid={`onboarding-step-${n}`}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-sm font-semibold">
          {n}. {title}
        </h2>
        {/* The word, not a colour or an icon. A tick that a screen reader cannot read is not a status. */}
        <span
          data-testid={`step-${n}-state`}
          className={
            state === "done"
              ? "text-xs font-medium text-emerald-600"
              : state === "todo" || state === "action"
                ? // Amber for both: each is something the user still has to do. The difference is
                  // whether a check confirmed it, and the badge word carries that.
                  "text-xs font-medium text-amber-600"
                : "text-xs font-medium text-muted-foreground"
          }
        >
          {badge}
        </span>
      </div>

      <p className="mt-2 text-sm text-muted-foreground">{state === "done" ? done : todo}</p>

      {(state === "action" || state === "no-route") && unknownNote ? (
        <p className="mt-1 text-xs text-muted-foreground">{unknownNote}</p>
      ) : null}

      {observedBy ? (
        <p className="mt-1 text-xs text-muted-foreground">
          Checked against <code>{observedBy}</code>.
        </p>
      ) : null}

      <p className="mt-2">
        <Link
          href={href}
          className="text-xs underline underline-offset-4 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {hrefLabel} →
        </Link>
      </p>
    </li>
  );
}
