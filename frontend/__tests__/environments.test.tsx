// SPDX-License-Identifier: FSL-1.1-ALv2
/**
 * §2.1's screens, and specifically the three places they could mislead an operator.
 *
 * 1. THE APPROVAL CONSEQUENCE IS IN WORDS, not in a checkbox state. An environment that deploys
 *    unattended says so wherever it is listed and wherever it is chosen. A reader skimming a list must
 *    not have to notice an unticked box to spot the dangerous one.
 * 2. "NONE CONFIGURED" AND "COULD NOT LOAD" ARE DIFFERENT SENTENCES. The first invites adding one; the
 *    second invites a retry. Collapsing them is how an operator adds a duplicate environment to a
 *    project that already has three, because the list appeared empty.
 * 3. A SECRET READS AS SET-BUT-WITHHELD, never as blank. A blank field for a configured secret is how a
 *    working value gets overwritten by someone saving a form they only meant to look at.
 *
 * The create form's fourth property is asserted too: an untouched approval checkbox sends `null`, not
 * `false`. Those are the same shape on the wire and opposite in meaning — `null` means "not stated" and
 * the server resolves it to "approval required", while `false` waives the gate.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  type Environment,
  EnvironmentManager,
  EnvironmentSelector,
  EnvironmentVariables,
} from "@/features/environments/EnvironmentManager";

const get = vi.fn();
const post = vi.fn();
const put = vi.fn();
const del = vi.fn();

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      get: (...args: unknown[]) => get(...args),
      post: (...args: unknown[]) => post(...args),
      put: (...args: unknown[]) => put(...args),
      delete: (...args: unknown[]) => del(...args),
    },
  };
});

const PROJECT = "11111111-1111-4111-8111-111111111111";

const DEV: Environment = {
  id: "aaaaaaaa-1111-4111-8111-111111111111",
  name: "dev",
  kind: "development",
  k8s_context: "kind-forgeops",
  requires_approval: false,
  position: 0,
};

const PROD: Environment = {
  id: "bbbbbbbb-1111-4111-8111-111111111111",
  name: "prod",
  kind: "production",
  k8s_context: null,
  requires_approval: true,
  position: 1,
};

function renderWithQuery(element: ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={client}>{element}</QueryClientProvider>);
}

beforeEach(() => {
  get.mockReset();
  post.mockReset();
  put.mockReset();
  del.mockReset();
});

describe("the environment list states the approval consequence", () => {
  it("says in words which environments deploy unattended", async () => {
    get.mockResolvedValue({ environments: [DEV, PROD] });

    renderWithQuery(<EnvironmentManager projectId={PROJECT} />);

    await waitFor(() => expect(screen.getByTestId("environment-list")).toBeInTheDocument());
    expect(screen.getByTestId("environment-gate-dev")).toHaveTextContent(/run unattended/i);
    expect(screen.getByTestId("environment-gate-prod")).toHaveTextContent(/need human approval/i);
  });

  it("reports an unwired Kubernetes context as unwired rather than as blank", async () => {
    get.mockResolvedValue({ environments: [PROD] });

    renderWithQuery(<EnvironmentManager projectId={PROJECT} />);

    await waitFor(() =>
      expect(screen.getByTestId("environment-prod")).toHaveTextContent(/no Kubernetes context/i),
    );
  });
});

describe("absence is distinguished from failure", () => {
  it("an empty project invites adding the first environment", async () => {
    get.mockResolvedValue({ environments: [] });

    renderWithQuery(<EnvironmentManager projectId={PROJECT} />);

    await waitFor(() => expect(screen.getByTestId("environments-empty")).toBeInTheDocument());
    expect(screen.getByTestId("environments-empty")).toHaveTextContent(/start of the pipeline/i);
  });

  it("a failed load says nothing has been changed, and does not render an empty list", async () => {
    get.mockRejectedValue(new Error("network down"));

    renderWithQuery(<EnvironmentManager projectId={PROJECT} />);

    await waitFor(() => expect(screen.getByTestId("environments-error")).toBeInTheDocument());
    expect(screen.queryByTestId("environments-empty")).not.toBeInTheDocument();
  });
});

describe("the create form", () => {
  it("sends null rather than false when the approval waiver was never touched", async () => {
    get.mockResolvedValue({ environments: [] });
    post.mockResolvedValue({ ...DEV, name: "staging" });

    renderWithQuery(<EnvironmentManager projectId={PROJECT} />);
    await waitFor(() => expect(screen.getByTestId("environments-empty")).toBeInTheDocument());

    await userEvent.type(screen.getByLabelText("Name"), "staging");
    await userEvent.click(screen.getByRole("button", { name: "Add environment" }));

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    // `null` and `false` are the same shape on the wire and opposite in meaning. This is the assertion
    // that keeps a UI which never asked from waiving a production gate.
    expect(post.mock.calls[0][1]).toMatchObject({ name: "staging", requires_approval: null });
  });

  it("sends false only when the waiver was ticked", async () => {
    get.mockResolvedValue({ environments: [] });
    post.mockResolvedValue(DEV);

    renderWithQuery(<EnvironmentManager projectId={PROJECT} />);
    await waitFor(() => expect(screen.getByTestId("environments-empty")).toBeInTheDocument());

    await userEvent.type(screen.getByLabelText("Name"), "scratch");
    await userEvent.click(screen.getByLabelText(/without human approval/i));
    await userEvent.click(screen.getByRole("button", { name: "Add environment" }));

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(post.mock.calls[0][1]).toMatchObject({ requires_approval: false });
  });

  it("shows the server's own refusal, which names the way forward", async () => {
    get.mockResolvedValue({ environments: [] });
    post.mockRejectedValue({
      problem: {
        detail:
          "an environment of kind 'production' cannot waive human approval. Create an environment of kind 'custom' if an unattended target is genuinely intended, so that what it is is visible in its kind.",
      },
    });

    renderWithQuery(<EnvironmentManager projectId={PROJECT} />);
    await waitFor(() => expect(screen.getByTestId("environments-empty")).toBeInTheDocument());

    await userEvent.type(screen.getByLabelText("Name"), "prod");
    await userEvent.click(screen.getByRole("button", { name: "Add environment" }));

    await waitFor(() => expect(screen.getByTestId("environment-problem")).toBeInTheDocument());
    // The useful half of the message: not "could not create" but what to do instead.
    expect(screen.getByTestId("environment-problem")).toHaveTextContent(/kind 'custom'/);
  });
});

describe("the selector", () => {
  it("names the approval consequence in each option", async () => {
    get.mockResolvedValue({ environments: [DEV, PROD] });

    renderWithQuery(<EnvironmentSelector projectId={PROJECT} value={null} onChange={() => {}} />);

    await waitFor(() => expect(screen.getByTestId("environment-selector")).toBeInTheDocument());
    expect(screen.getByRole("option", { name: /dev .*deploys unattended/i })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /prod .*needs approval/i })).toBeInTheDocument();
  });

  it("reports a load failure as a failure rather than as no environments", async () => {
    get.mockRejectedValue(new Error("boom"));

    renderWithQuery(<EnvironmentSelector projectId={PROJECT} value={null} onChange={() => {}} />);

    await waitFor(() =>
      expect(screen.getByTestId("environment-selector-error")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("environment-selector-error")).toHaveTextContent(
      /not the same as having none/i,
    );
  });

  it("tells the caller which environment was chosen", async () => {
    get.mockResolvedValue({ environments: [DEV, PROD] });
    const onChange = vi.fn();

    renderWithQuery(<EnvironmentSelector projectId={PROJECT} value={null} onChange={onChange} />);

    await waitFor(() => expect(screen.getByTestId("environment-selector")).toBeInTheDocument());
    await userEvent.selectOptions(screen.getByTestId("environment-selector"), PROD.id);

    expect(onChange).toHaveBeenCalledWith(PROD.id);
  });

  it("says a project has none when it genuinely has none", async () => {
    get.mockResolvedValue({ environments: [] });

    renderWithQuery(<EnvironmentSelector projectId={PROJECT} value={null} onChange={() => {}} />);

    await waitFor(() =>
      expect(screen.getByTestId("environment-selector-empty")).toBeInTheDocument(),
    );
  });
});

describe("variables", () => {
  it("shows a secret as set and withheld, never as an empty value", async () => {
    get.mockResolvedValue({
      variables: [
        { key: "DATABASE_URL", value: null, is_secret: true },
        { key: "LOG_LEVEL", value: "debug", is_secret: false },
      ],
    });

    renderWithQuery(<EnvironmentVariables projectId={PROJECT} environmentId={PROD.id} />);

    await waitFor(() => expect(screen.getByTestId("variable-list")).toBeInTheDocument());
    expect(screen.getByTestId("variable-DATABASE_URL")).toHaveTextContent(/set, not shown/i);
    expect(screen.getByTestId("variable-LOG_LEVEL")).toHaveTextContent("debug");
  });

  it("masks the input while a secret is being typed", async () => {
    get.mockResolvedValue({ variables: [] });

    renderWithQuery(<EnvironmentVariables projectId={PROJECT} environmentId={PROD.id} />);
    await waitFor(() => expect(screen.getByTestId("variables-empty")).toBeInTheDocument());

    await userEvent.click(screen.getByLabelText(/Store as a secret/i));

    expect(screen.getByLabelText("Value")).toHaveAttribute("type", "password");
  });

  it("sends the secret flag the operator chose", async () => {
    get.mockResolvedValue({ variables: [] });
    put.mockResolvedValue({ key: "TOKEN_X", value: null, is_secret: true });

    renderWithQuery(<EnvironmentVariables projectId={PROJECT} environmentId={PROD.id} />);
    await waitFor(() => expect(screen.getByTestId("variables-empty")).toBeInTheDocument());

    await userEvent.type(screen.getByLabelText("Key"), "TOKEN_X");
    await userEvent.click(screen.getByLabelText(/Store as a secret/i));
    await userEvent.type(screen.getByLabelText("Value"), "s3cret");
    await userEvent.click(screen.getByRole("button", { name: "Save variable" }));

    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
    expect(put.mock.calls[0][1]).toMatchObject({ key: "TOKEN_X", is_secret: true });
  });
});
