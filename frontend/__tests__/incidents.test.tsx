/**
 * The incident and RCA panels. Phase 2 §2.11.
 *
 * THE ASSERTIONS THAT MATTER ARE THE NEGATIVE ONES. An analysis with state `insufficient_evidence` or
 * `unavailable` has empty problem/location/fix fields, and the failure this suite exists to prevent is
 * rendering those three empty fields under a "Root cause" heading -- which a human reads as "analysed,
 * nothing found". So the tests check that no problem/location markup appears at all in those states, not
 * merely that the explanatory sentence is present.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  IncidentDetail,
  IncidentList,
  SuggestionDiff,
  type AnalysisRow,
  type AnalysisState,
  type EvidenceRow,
  type IncidentListRow,
} from "@/features/incidents/IncidentPanels";

const get = vi.fn();

vi.mock("@/lib/api/client", () => ({
  api: { get: (...args: unknown[]) => get(...args) },
}));

function mount(element: ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={client}>{element}</QueryClientProvider>);
}

function incident(overrides: Partial<IncidentListRow> = {}): IncidentListRow {
  return {
    id: "11111111-1111-4111-8111-111111111111",
    source: "container_exit",
    severity: "critical",
    title: "container api exited 137",
    occurrences: 1,
    detected_at: "2026-09-23T00:00:00Z",
    last_seen_at: "2026-09-23T00:00:00Z",
    resolved_at: null,
    origin_kind: "container",
    origin_id: "api",
    analysis_state: "not_analysed",
    evidence_reachable: null,
    evidence_consulted: null,
    ...overrides,
  };
}

function analysis(overrides: Partial<AnalysisRow> = {}): AnalysisRow {
  return {
    id: "aaaa1111-1111-4111-8111-111111111111",
    state: "analysed",
    problem: "The container exceeded its memory limit.",
    location: "deploy/api.yaml, resources.limits.memory",
    fix: "Raise the limit to 512Mi.",
    evidence_reachable: 3,
    evidence_consulted: 3,
    model: "ollama-primary",
    served_from: "provider",
    created_at: "2026-09-23T00:01:00Z",
    ...overrides,
  };
}

function evidence(reachable: boolean, kind = "metrics"): EvidenceRow {
  return {
    kind,
    reachable,
    summary: reachable ? `${kind} answered.` : `${kind} could not be read.`,
    detail: {},
    collected_at: "2026-09-23T00:00:30Z",
  };
}

function detailResponse(
  overrides: {
    analyses?: AnalysisRow[];
    evidence?: EvidenceRow[];
    suggestions?: unknown[];
    caveat?: string;
  } = {},
) {
  return {
    incident: { ...incident(), detail: {}, fingerprint: "abc" },
    evidence: overrides.evidence ?? [evidence(true), evidence(true, "logs")],
    analyses: overrides.analyses ?? [],
    suggestions: overrides.suggestions ?? [],
    evidence_caveat: overrides.caveat ?? "Every evidence source consulted was readable.",
  };
}

beforeEach(() => {
  get.mockReset();
});

describe("the incident list", () => {
  it("says an empty list means nothing was ingested, not that nothing failed", async () => {
    get.mockResolvedValue({
      incidents: [],
      explanation:
        "No incidents have been recorded for this project. That means nothing has been INGESTED -- which is not the same as nothing having failed, if the sources that feed ingestion are not running.",
    });
    mount(<IncidentList projectId="p1" />);
    const empty = await screen.findByTestId("incident-list-empty");
    expect(empty.textContent).toContain("not the same as nothing having failed");
    // And no table, which would read as a measured zero.
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("distinguishes a failed request from an empty list", async () => {
    get.mockRejectedValue(new Error("network"));
    mount(<IncidentList projectId="p1" />);
    const error = await screen.findByTestId("incident-list-error");
    expect(error.textContent).toContain("not known whether anything has failed");
    expect(error.textContent).toContain("empty list is not being shown");
  });

  it("states occurrences with a unit rather than as a bare number", async () => {
    get.mockResolvedValue({
      incidents: [incident({ occurrences: 47 }), incident({ id: "b", occurrences: 1 })],
      explanation: "2 incident(s) recorded for this project.",
    });
    mount(<IncidentList projectId="p1" />);
    await waitFor(() => expect(screen.getByTestId("incident-occurrences-b")).toBeInTheDocument());
    expect(screen.getByTestId("incident-occurrences-b").textContent).toBe("once");
    expect(
      screen.getByTestId("incident-occurrences-11111111-1111-4111-8111-111111111111").textContent,
    ).toBe("47 times");
  });

  it("shows the analysis state on the row so it need not be opened", async () => {
    get.mockResolvedValue({
      incidents: [
        incident({
          analysis_state: "insufficient_evidence",
          evidence_reachable: 1,
          evidence_consulted: 3,
        }),
      ],
      explanation: "1 incident(s).",
    });
    mount(<IncidentList projectId="p1" />);
    const cell = await screen.findByTestId(
      "incident-analysis-11111111-1111-4111-8111-111111111111",
    );
    expect(cell.textContent).toContain("Not enough evidence");
    expect(cell.textContent).toContain("1 of 3 sources");
  });
});

describe("the RCA display shows a cause only when there is one", () => {
  it.each([
    ["not_analysed", "has not been analysed"],
    ["unavailable", "no model could be reached"],
    ["insufficient_evidence", "Too few evidence sources"],
    ["inconclusive", "does not support identifying a cause"],
  ] as Array<[AnalysisState, string]>)(
    "%s renders its sentence and NO problem or location",
    async (state, phrase) => {
      get.mockResolvedValue(
        detailResponse({
          analyses: [analysis({ state, problem: "", location: "", fix: "" })],
        }),
      );
      mount(<IncidentDetail incidentId="i1" />);
      const stateEl = await screen.findByTestId("incident-rca-state");
      expect(stateEl.textContent).toContain(phrase);
      // THE ASSERTION THAT MATTERS: the problem/location block is absent entirely, not empty.
      expect(screen.queryByTestId("incident-rca-content")).toBeNull();
      expect(screen.queryByText("Problem")).toBeNull();
      expect(screen.queryByText("Location")).toBeNull();
    },
  );

  it("renders problem, location and fix when a cause was identified", async () => {
    get.mockResolvedValue(detailResponse({ analyses: [analysis()] }));
    mount(<IncidentDetail incidentId="i1" />);
    const content = await screen.findByTestId("incident-rca-content");
    expect(content.textContent).toContain("exceeded its memory limit");
    expect(content.textContent).toContain("deploy/api.yaml");
    expect(content.textContent).toContain("512Mi");
  });

  it("says so when a cause was found but no remedy proposed", async () => {
    get.mockResolvedValue(detailResponse({ analyses: [analysis({ fix: "" })] }));
    mount(<IncidentDetail incidentId="i1" />);
    const none = await screen.findByTestId("incident-rca-no-fix");
    expect(none.textContent).toContain("what to do about it was not");
  });

  it("names the model, or says none answered", async () => {
    get.mockResolvedValue(
      detailResponse({
        analyses: [
          analysis({ state: "unavailable", problem: "", location: "", fix: "", model: "" }),
        ],
      }),
    );
    mount(<IncidentDetail incidentId="i1" />);
    const provenance = await screen.findByTestId("incident-rca-provenance");
    expect(provenance.textContent).toContain("No model answered");
    expect(provenance.textContent).toContain("nothing here was inferred by one");
  });

  it("renders the not-analysed sentence when there is no analysis row at all", async () => {
    get.mockResolvedValue(detailResponse({ analyses: [] }));
    mount(<IncidentDetail incidentId="i1" />);
    const none = await screen.findByTestId("incident-rca-none");
    expect(none.textContent).toContain("different from nothing being wrong");
  });
});

describe("evidence is shown whether or not it answered", () => {
  it("lists unreachable sources rather than omitting them", async () => {
    get.mockResolvedValue(
      detailResponse({
        evidence: [evidence(true), evidence(false, "logs"), evidence(false, "kubernetes")],
        caveat:
          "2 of 3 evidence sources could not be read (kubernetes, logs). Any conclusion below was reached without them.",
      }),
    );
    mount(<IncidentDetail incidentId="i1" />);
    await waitFor(() => expect(screen.getByTestId("evidence-logs-1")).toBeInTheDocument());
    expect(screen.getByTestId("evidence-kubernetes-2").textContent).toContain("could not be read");
    // The caveat is an alert when sources were missed, so it is announced rather than merely present.
    expect(screen.getByTestId("incident-evidence-caveat").getAttribute("role")).toBe("alert");
  });

  it("is a status rather than an alert when every source answered", async () => {
    get.mockResolvedValue(detailResponse({}));
    mount(<IncidentDetail incidentId="i1" />);
    const caveat = await screen.findByTestId("incident-evidence-caveat");
    expect(caveat.getAttribute("role")).toBe("status");
  });

  it("says nothing was collected rather than rendering an empty table", async () => {
    get.mockResolvedValue(
      detailResponse({ evidence: [], caveat: "No evidence has been collected for this incident." }),
    );
    mount(<IncidentDetail incidentId="i1" />);
    const none = await screen.findByTestId("incident-evidence-none");
    expect(none.textContent).toContain("was observed and recorded");
  });
});

describe("the diff preview", () => {
  it("warns that the comparison is against what the file held when analysed", async () => {
    get.mockResolvedValue({
      path: "deploy/api.yaml",
      rationale: "Raise the memory limit.",
      diff: [
        "--- a/deploy/api.yaml",
        "+++ b/deploy/api.yaml",
        "-  memory: 256Mi",
        "+  memory: 512Mi",
      ],
      already_submitted: false,
      change_set_id: null,
      caveat:
        "This diff compares the proposal against what the file held WHEN THE ANALYSIS RAN, which may no longer be what it holds.",
    });
    mount(<SuggestionDiff incidentId="i1" suggestionId="s1" />);
    const caveat = await screen.findByTestId("diff-caveat-s1");
    expect(caveat.textContent).toContain("WHEN THE ANALYSIS RAN");
    // Asserted on the code block's text rather than via getByText: the DOM matcher normalises runs of
    // whitespace, so the leading spaces a diff depends on are collapsed before the match is tried.
    expect(screen.getByTestId("diff-s1").querySelector("code")?.textContent).toContain(
      "+  memory: 512Mi",
    );
  });

  it("says there is no difference rather than showing an empty diff to approve", async () => {
    get.mockResolvedValue({
      path: "deploy/api.yaml",
      rationale: "",
      diff: [],
      already_submitted: false,
      change_set_id: null,
      caveat: "c",
    });
    mount(<SuggestionDiff incidentId="i1" suggestionId="s1" />);
    const empty = await screen.findByTestId("diff-empty-s1");
    expect(empty.textContent).toContain("there is no difference");
  });

  it("does not imply a suggestion was applied when the diff cannot be rendered", async () => {
    get.mockRejectedValue(new Error("boom"));
    mount(<SuggestionDiff incidentId="i1" suggestionId="s1" />);
    const error = await screen.findByTestId("diff-error-s1");
    expect(error.textContent).toContain("has not been applied");
  });

  it("marks a submitted suggestion as awaiting approval, not as applied", async () => {
    get.mockImplementation((path: string) => {
      if (path.includes("/preview")) {
        return Promise.resolve({
          path: "deploy/api.yaml",
          rationale: "",
          diff: ["+x"],
          already_submitted: true,
          change_set_id: "cs-1",
          caveat: "c",
        });
      }
      return Promise.resolve(
        detailResponse({
          analyses: [analysis()],
          suggestions: [
            {
              id: "s1",
              analysis_id: "a1",
              path: "deploy/api.yaml",
              rationale: "",
              change_set_id: "cs-1",
              created_at: "2026-09-23T00:02:00Z",
              proposed_bytes: 40,
            },
          ],
        }),
      );
    });
    mount(<IncidentDetail incidentId="i1" />);
    const marker = await screen.findByTestId("suggestion-submitted-s1");
    expect(marker.textContent).toContain("awaiting the same approval as any other mutation");
    // Never the word "applied" in the affirmative.
    expect(marker.textContent).not.toContain("applied");
  });
});
