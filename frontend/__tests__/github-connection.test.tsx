// SPDX-License-Identifier: FSL-1.1-ALv2
/**
 * The GitHub connection panel's three states and its two mutations.
 *
 * WHAT THESE PIN, in the order they matter:
 *
 * * the UNCONFIGURED state renders the settings to set rather than an error. That is the state of
 *   every fresh install, and the previous generation of this product's screens rendered exactly this
 *   situation as a bare failure, which sends a user looking for a fault that does not exist.
 * * the tri-state last-use line. `null` is "never used", not "fine" and not "broken" — the same
 *   discipline the pairing screen's heartbeat uses, and the reason is that a human acts on it.
 * * a failed revocation is SAID, not swallowed: the local link is gone and the GitHub token may still
 *   be live, which is a different fact with a different remedy.
 * * no token ever reaches the DOM, asserted over the rendered container rather than field by field so
 *   a later field cannot carry one past this test.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  describeLastUse,
  GitHubConnection,
  type GitHubLinkStatus,
} from "@/features/integrations/GitHubConnection";

const get = vi.fn();
const post = vi.fn();
const del = vi.fn();

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      get: (...args: unknown[]) => get(...args),
      post: (...args: unknown[]) => post(...args),
      delete: (...args: unknown[]) => del(...args),
    },
  };
});

const CONNECTED: GitHubLinkStatus = {
  configured: true,
  connected: true,
  configuration_hint: "",
  login: "octo-cat",
  avatar_url: null,
  scopes: [],
  connected_at: "2026-09-19T10:00:00+00:00",
  last_use_ok: null,
  last_used_at: null,
  last_use_detail: "",
  access_token_expires_at: "2026-09-19T18:00:00+00:00",
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
  del.mockReset();
});

describe("GitHubConnection", () => {
  it("renders what to configure when the server has no GitHub App", async () => {
    get.mockResolvedValue({
      ...CONNECTED,
      configured: false,
      connected: false,
      login: null,
      configuration_hint:
        "Set GITHUB_APP_CLIENT_ID and GITHUB_APP_OAUTH_CREDENTIAL, then register …",
    });

    renderWithQuery(<GitHubConnection />);

    expect(await screen.findByTestId("github-link-unconfigured")).toBeInTheDocument();
    expect(screen.getByTestId("github-link-hint")).toHaveTextContent("GITHUB_APP_CLIENT_ID");
    // Not an error: a fresh install is not a fault, and rendering one sends a user hunting.
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByTestId("github-connect")).not.toBeInTheDocument();
  });

  it("offers a connect control when configured but not connected", async () => {
    get.mockResolvedValue({ ...CONNECTED, connected: false, login: null });
    post.mockResolvedValue({ authorize_url: "https://github.test/login", expires_in_seconds: 600 });
    const assign = vi.fn();
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { ...window.location, assign },
    });

    renderWithQuery(<GitHubConnection />);
    await userEvent.click(await screen.findByTestId("github-connect"));

    await waitFor(() => expect(post).toHaveBeenCalledWith("/integrations/github/connect"));
    // The browser navigates; a fetch-followed redirect would be consumed by the fetch and the user
    // would never see GitHub's authorization page.
    await waitFor(() => expect(assign).toHaveBeenCalledWith("https://github.test/login"));
  });

  it("names the connected account and never renders a token", async () => {
    get.mockResolvedValue(CONNECTED);

    const { container } = renderWithQuery(<GitHubConnection />);

    expect(await screen.findByTestId("github-link-connected")).toHaveTextContent("octo-cat");
    expect(screen.getByTestId("github-last-use")).toHaveTextContent("never used");
    expect(container.textContent).not.toMatch(/gh[pousr]_/);
  });

  it("disconnects and refreshes the status", async () => {
    get.mockResolvedValue(CONNECTED);
    del.mockResolvedValue({ login: "octo-cat", revoked_at_github: true });

    renderWithQuery(<GitHubConnection />);
    await userEvent.click(await screen.findByTestId("github-disconnect"));

    await waitFor(() => expect(del).toHaveBeenCalledWith("/integrations/github"));
    expect(screen.queryByTestId("github-link-problem")).not.toBeInTheDocument();
  });

  it("says so when GitHub did not confirm the revocation", async () => {
    get.mockResolvedValue(CONNECTED);
    del.mockResolvedValue({ login: "octo-cat", revoked_at_github: false });

    renderWithQuery(<GitHubConnection />);
    await userEvent.click(await screen.findByTestId("github-disconnect"));

    const problem = await screen.findByTestId("github-link-problem");
    expect(problem).toHaveTextContent("did not confirm");
    expect(problem).toHaveTextContent("GitHub");
  });

  it("reports a failed status read rather than rendering an empty panel", async () => {
    get.mockRejectedValue(new Error("network down"));

    renderWithQuery(<GitHubConnection />);

    expect(await screen.findByTestId("github-link-error")).toHaveTextContent("network down");
  });
});

describe("describeLastUse", () => {
  it("distinguishes never used from worked and from failed", () => {
    expect(describeLastUse({ ...CONNECTED, last_use_ok: null })).toContain("never used");
    expect(
      describeLastUse({
        ...CONNECTED,
        last_use_ok: true,
        last_used_at: "2026-09-19T11:00:00+00:00",
        last_use_detail: "listed 2 repository(ies)",
      }),
    ).toContain("worked at 2026-09-19T11:00:00+00:00");
    expect(
      describeLastUse({
        ...CONNECTED,
        last_use_ok: false,
        last_used_at: "2026-09-19T11:00:00+00:00",
        last_use_detail: "GitHub refused to list the repositories (HTTP 401)",
      }),
    ).toContain("failed at");
  });
});
