// SPDX-License-Identifier: FSL-1.1-ALv2
/**
 * The policy list, the Rego editor, its validation feedback and the dry run —
 * phases.md §1.7 "Frontend: Policy list and editor UI".
 *
 * Three things this asserts that a smoke test would not:
 *
 *  1. **The validator's own message survives.** `validate_rego` answers 422 with
 *     `[rego_parse_error] <message> at line N`, and the line number is the only part an author needs.
 *     A page that rendered "Could not save" over it would throw that away, so the test asserts the
 *     detail appears verbatim.
 *  2. **A dry-run result is attributed.** The response carries the query evaluated and the evaluator's
 *     version, and both must be on screen — because this endpoint used to synthesise a decision when
 *     OPA was absent, and an unattributed decision is indistinguishable from that.
 *  3. **`undefined` is not a deny.** The old backend defaulted an undefined rule to "deny", hiding a
 *     misspelled package behind something that looks like enforcement.
 */

import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const { mockGet, mockPost, mockPatch, mockDelete } = vi.hoisted(() => ({
  mockGet: vi.fn(),
  mockPost: vi.fn(),
  mockPatch: vi.fn(),
  mockDelete: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      get: mockGet,
      post: mockPost,
      patch: mockPatch,
      put: vi.fn(),
      delete: mockDelete,
      deleteWith: vi.fn(),
      stream: vi.fn(),
    },
  };
});

vi.mock("next/navigation", () => ({ usePathname: () => "/policies" }));

import PoliciesPage from "@/app/(shell)/policies/page";
import { ApiProblemError } from "@/lib/api";

const POLICY = {
  id: "pol-1",
  project_id: null,
  tenant_id: null,
  name: "No Friday deploys",
  engine: "rego",
  rego_rules: 'package forgeops.governance\ndefault decision = "deny"\n',
  enabled: true,
  template_id: "scheduling",
  created_at: "2026-08-20T00:00:00Z",
  updated_at: "2026-08-21T00:00:00Z",
};

const TEMPLATE = {
  id: "scheduling",
  name: "Never deploy on Fridays",
  description: "Refuses an apply between Friday and Sunday.",
  rego_rules: 'package forgeops.governance\ndefault decision = "deny"\n',
  parameters: {},
};

function renderPage() {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0, staleTime: 0 },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={client}>
      <PoliciesPage />
    </QueryClientProvider>,
  );
}

function serve(policies = [POLICY], templates = [TEMPLATE]) {
  mockGet.mockImplementation((path: string) =>
    path.startsWith("/policies/templates")
      ? Promise.resolve(templates)
      : Promise.resolve({ policies, next_cursor: null }),
  );
}

beforeEach(() => {
  for (const m of [mockGet, mockPost, mockPatch, mockDelete]) m.mockReset();
});
afterEach(() => cleanup());

describe("the policy list", () => {
  it("reads the list route that did not exist until this screen needed it", async () => {
    serve();
    renderPage();
    await waitFor(() =>
      expect(mockGet).toHaveBeenCalledWith(expect.stringContaining("/policies?limit=")),
    );
    expect(await screen.findByText("No Friday deploys")).toBeInTheDocument();
  });

  it("reports whether each policy is enabled and how widely it applies", async () => {
    serve();
    renderPage();
    expect(await screen.findByTestId("policy-enabled-pol-1")).toHaveTextContent("enabled");
    // `project_id: null` means global. Rendering that as blank would leave the scope unknowable.
    expect(screen.getByText(/applies to every project in this tenant/i)).toBeInTheDocument();
  });

  it("explains the consequence of having no policies at all", async () => {
    serve([]);
    renderPage();
    // An absent policy is a DENY, not permission. A generic "nothing here" would let someone conclude
    // the chokepoint is permissive until configured.
    expect(
      await screen.findByText(/an absent policy is a deny, not permission/i),
    ).toBeInTheDocument();
  });

  it("deletes only after confirmation, and says what a published bundle keeps", async () => {
    serve();
    mockDelete.mockResolvedValue(undefined);
    renderPage();

    await userEvent.click(await screen.findByTestId("delete-policy-pol-1"));
    expect(mockDelete).not.toHaveBeenCalled();
    // Superseded bundles are kept, so a device pinned to an old digest is unaffected until a new
    // bundle is published. Somebody deleting a rule needs to know it is still in force.
    expect(screen.getByText(/keeps the rule it compiled/i)).toBeInTheDocument();

    await userEvent.click(screen.getByTestId("confirm-delete-policy"));
    await waitFor(() => expect(mockDelete).toHaveBeenCalledWith("/policies/pol-1"));
  });
});

describe("authoring a policy", () => {
  it("creates with POST and a Rego body", async () => {
    serve([]);
    mockPost.mockResolvedValue(POLICY);
    renderPage();

    await userEvent.click(await screen.findByTestId("new-policy"));
    await userEvent.type(screen.getByLabelText("Name"), "No Friday deploys");
    await userEvent.click(screen.getByTestId("save-policy"));

    await waitFor(() =>
      expect(mockPost).toHaveBeenCalledWith(
        "/policies",
        expect.objectContaining({ name: "No Friday deploys", engine: "rego", enabled: true }),
      ),
    );
  });

  it("updates an existing policy with PATCH rather than a second create", async () => {
    serve();
    mockPatch.mockResolvedValue(POLICY);
    renderPage();

    await userEvent.click(await screen.findByTestId("edit-pol-1"));
    await userEvent.click(screen.getByTestId("save-policy"));

    await waitFor(() =>
      expect(mockPatch).toHaveBeenCalledWith(
        "/policies/pol-1",
        expect.objectContaining({
          name: "No Friday deploys",
        }),
      ),
    );
    expect(mockPost).not.toHaveBeenCalled();
  });

  it("copies a template's Rego rather than linking to it", async () => {
    serve([]);
    renderPage();

    await userEvent.click(await screen.findByTestId("new-policy"));
    await userEvent.selectOptions(screen.getByLabelText(/start from a template/i), "scheduling");

    const editor = screen.getByTestId("policy-rego") as HTMLTextAreaElement;
    expect(editor.value).toContain('default decision = "deny"');
    // Copied, so editing your policy cannot change the template. The page says so.
    expect(screen.getByText(/it does not link to it/i)).toBeInTheDocument();
  });

  it("resets the editor when a different policy is selected", async () => {
    const second = {
      ...POLICY,
      id: "pol-2",
      name: "Protect package.json",
      rego_rules: "package other\n",
    };
    serve([POLICY, second]);
    renderPage();

    await userEvent.click(await screen.findByTestId("edit-pol-1"));
    expect((screen.getByTestId("policy-rego") as HTMLTextAreaElement).value).toContain(
      "forgeops.governance",
    );

    await userEvent.click(screen.getByTestId("edit-pol-2"));
    // Without the reset, the first policy's Rego would sit in the textarea under the second's name —
    // and saving would write one policy's rules over another's.
    await waitFor(() =>
      expect((screen.getByTestId("policy-rego") as HTMLTextAreaElement).value).toContain(
        "package other",
      ),
    );
    expect(screen.getByLabelText("Name")).toHaveValue("Protect package.json");
  });

  it("turns off the input assists that would corrupt Rego", async () => {
    serve([]);
    renderPage();
    await userEvent.click(await screen.findByTestId("new-policy"));
    const editor = screen.getByTestId("policy-rego");
    // An editor that autocapitalises `package` produces Rego that does not compile, and the author
    // gets blamed for it.
    expect(editor).toHaveAttribute("autocapitalize", "off");
    expect(editor).toHaveAttribute("autocorrect", "off");
    expect(editor).toHaveAttribute("spellcheck", "false");
  });
});

describe("validation feedback", () => {
  it("shows the validator's own message, with its rule code and line, verbatim", async () => {
    serve([]);
    mockPost.mockRejectedValue(
      new ApiProblemError({
        type: "https://errors.forgeops.dev/validation-failed",
        title: "Request validation failed",
        status: 422,
        detail: "[rego_parse_error] unexpected assign token at line 3",
      }),
    );
    renderPage();

    await userEvent.click(await screen.findByTestId("new-policy"));
    await userEvent.type(screen.getByLabelText("Name"), "Broken");
    await userEvent.click(screen.getByTestId("save-policy"));

    const panel = await screen.findByTestId("rego-validation-error");
    // The line number is the only part that helps. Paraphrasing throws it away.
    expect(panel).toHaveTextContent("[rego_parse_error] unexpected assign token at line 3");
    expect(panel).toHaveAttribute("role", "alert");
    expect(panel).toHaveTextContent(/nothing was saved/i);
  });

  it("marks the editor invalid so the failure is associated with the field", async () => {
    serve([]);
    mockPost.mockRejectedValue(
      new ApiProblemError({
        type: "https://errors.forgeops.dev/validation-failed",
        title: "Request validation failed",
        status: 422,
        detail: "[rego_parse_error] bad",
      }),
    );
    renderPage();
    await userEvent.click(await screen.findByTestId("new-policy"));
    await userEvent.type(screen.getByLabelText("Name"), "Broken");
    await userEvent.click(screen.getByTestId("save-policy"));

    await waitFor(() =>
      expect(screen.getByTestId("policy-rego")).toHaveAttribute("aria-invalid", "true"),
    );
  });

  it("renders a non-validation failure through the governance explanation instead", async () => {
    serve([]);
    mockPost.mockRejectedValue(
      new ApiProblemError({
        type: "https://errors.forgeops.dev/forbidden",
        title: "Forbidden",
        status: 403,
        detail: "You do not have permission to perform this action.",
      }),
    );
    renderPage();
    await userEvent.click(await screen.findByTestId("new-policy"));
    await userEvent.type(screen.getByLabelText("Name"), "X");
    await userEvent.click(screen.getByTestId("save-policy"));

    // A 403 is not a syntax problem, so it must not be presented as one.
    expect(await screen.findByTestId("governance-refusal")).toBeInTheDocument();
    expect(screen.queryByTestId("rego-validation-error")).not.toBeInTheDocument();
  });
});

describe("the dry run", () => {
  async function openEditorOnStoredPolicy() {
    serve();
    renderPage();
    await userEvent.click(await screen.findByTestId("edit-pol-1"));
  }

  it("posts the parsed input document to the test endpoint", async () => {
    await openEditorOnStoredPolicy();
    mockPost.mockResolvedValue({
      decision: "allow",
      rule: "data.forgeops.governance.decision",
      evaluated_with: "opa 1.4.2",
      undefined: false,
    });

    await userEvent.click(screen.getByTestId("run-dryrun"));
    await waitFor(() =>
      expect(mockPost).toHaveBeenCalledWith("/policies/pol-1/test", {
        input: { action: "write_file", blast_radius: "workspace" },
      }),
    );
  });

  it("shows the decision together with what produced it", async () => {
    await openEditorOnStoredPolicy();
    mockPost.mockResolvedValue({
      decision: "allow",
      rule: "data.forgeops.governance.decision",
      evaluated_with: "opa 1.4.2",
      undefined: false,
    });

    await userEvent.click(screen.getByTestId("run-dryrun"));
    expect(await screen.findByTestId("dryrun-decision")).toHaveTextContent("allow");
    // The attribution. This endpoint used to answer with a synthesised allow/deny when OPA was absent,
    // and a decision you cannot attribute is indistinguishable from that.
    expect(screen.getByTestId("dryrun-evaluator")).toHaveTextContent("opa 1.4.2");
    // Scoped to the result panel: the rule name also appears in the editor's own help text, which
    // explains which query the chokepoint evaluates. Both mentions are wanted.
    expect(screen.getByTestId("dryrun-result")).toHaveTextContent(
      "data.forgeops.governance.decision",
    );
  });

  it("distinguishes an undefined rule from a deny", async () => {
    await openEditorOnStoredPolicy();
    mockPost.mockResolvedValue({
      decision: "undefined",
      rule: "data.forgeops.governance.decision",
      evaluated_with: "opa 1.4.2",
      undefined: true,
    });

    await userEvent.click(screen.getByTestId("run-dryrun"));
    const result = await screen.findByTestId("dryrun-result");
    expect(result).toHaveTextContent(/undefined, which is not a deny/i);
    // And it explains the practical consequence: the chokepoint DOES refuse an undefined decision, so
    // the effect is the same and the cause is not.
    expect(result).toHaveTextContent(/is treated as a refusal/i);
    expect(result).toHaveTextContent(/package name or a rule name that does not match/i);
  });

  it("reports a malformed input document itself rather than sending it", async () => {
    await openEditorOnStoredPolicy();
    const input = screen.getByTestId("dryrun-input");
    await userEvent.clear(input);
    await userEvent.type(input, "{{not json");

    await userEvent.click(screen.getByTestId("run-dryrun"));
    // Named as a local parse failure. Sending it would produce a 422 about a body the server could not
    // read, which says less than the parser's own message.
    expect(await screen.findByTestId("dryrun-local-error")).toHaveTextContent(/not valid JSON/i);
    expect(mockPost).not.toHaveBeenCalled();
  });

  it("explains a 503 as a deployment fault rather than as a decision", async () => {
    await openEditorOnStoredPolicy();
    mockPost.mockRejectedValue(
      new ApiProblemError({
        type: "https://errors.forgeops.dev/dryrun-unavailable",
        title: "Dry run unavailable",
        status: 503,
        detail: "The 'opa' binary is not on PATH, so this policy cannot be evaluated.",
      }),
    );

    await userEvent.click(screen.getByTestId("run-dryrun"));
    const refusal = await screen.findByTestId("governance-refusal");
    expect(refusal).toHaveTextContent(/could not be evaluated/i);
    expect(refusal).toHaveTextContent(/used to synthesise an allow or deny/i);
    // And emphatically no decision is displayed, because there is none.
    expect(screen.queryByTestId("dryrun-decision")).not.toBeInTheDocument();
  });
});

describe("publishing the bundle", () => {
  it("says why the step matters before it is taken", async () => {
    serve();
    renderPage();
    // The step that is easiest to skip and hardest to diagnose: without a bundle the chokepoint refuses
    // every submission, four layers from where the error surfaces.
    expect(
      await screen.findByText(/nothing downstream works until you have done this at least once/i),
    ).toBeInTheDocument();
  });

  it("posts the publish and reports the digest as accepted rather than live", async () => {
    serve();
    mockPost.mockResolvedValue({ digest: "sha256:abc", status: "publishing" });
    renderPage();

    await userEvent.click(await screen.findByTestId("publish-bundle"));
    await waitFor(() => expect(mockPost).toHaveBeenCalledWith("/policies/publish"));

    const result = await screen.findByTestId("publish-result");
    expect(result).toHaveTextContent("sha256:abc");
    // 202: activation is dispatched as a task, so claiming every agent has it would be a lie.
    expect(result).toHaveTextContent(/says the publish was accepted/i);
  });

  it("explains a refusal to publish rather than failing silently", async () => {
    serve();
    mockPost.mockRejectedValue(
      new ApiProblemError({
        type: "https://errors.forgeops.dev/forbidden",
        title: "Forbidden",
        status: 403,
      }),
    );
    renderPage();
    await userEvent.click(await screen.findByTestId("publish-bundle"));
    expect(await screen.findByTestId("governance-refusal")).toBeInTheDocument();
  });
});
