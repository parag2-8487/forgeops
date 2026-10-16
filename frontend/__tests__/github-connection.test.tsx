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

const CONNECTED: GitHubLinkStatus = {
  configured: true,
  connected: true,
  token_link_available: true,
  configuration_hint: "",
  login: "octo-cat",
  avatar_url: null,
  scopes: [],
  credential_kind: "oauth_app",
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
  put.mockReset();
  del.mockReset();
});

describe("GitHubConnection", () => {
  it("offers the redirect-free token path even when the server has no GitHub App", async () => {
    get.mockResolvedValue({
      ...CONNECTED,
      configured: false,
      connected: false,
      login: null,
      credential_kind: null,
      configuration_hint:
        "Set GITHUB_APP_CLIENT_ID and GITHUB_APP_OAUTH_CREDENTIAL, then register …",
    });

    renderWithQuery(<GitHubConnection />);

    // THE POINT: an unconfigured deployment still has a working way to link, and it is the one that
    // never leaves this page. The panel this replaced rendered instructions and no control at all.
    expect(await screen.findByTestId("github-token-form")).toBeInTheDocument();
    expect(screen.getByTestId("github-link-hint")).toHaveTextContent("GITHUB_APP_CLIENT_ID");
    // And the redirecting path is not offered, because it cannot work here.
    expect(screen.queryByTestId("github-connect")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("links with a pasted token and never sends the user to GitHub", async () => {
    get.mockResolvedValue({ ...CONNECTED, connected: false, login: null, credential_kind: null });
    put.mockResolvedValue({ ...CONNECTED, credential_kind: "personal_token" });
    const assign = vi.fn();
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { ...window.location, assign },
    });

    renderWithQuery(<GitHubConnection />);
    await userEvent.type(
      await screen.findByTestId("github-token-input"),
      "a-github-token-for-this-test",
    );
    await userEvent.click(screen.getByTestId("github-token-submit"));

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith("/integrations/github/token", {
        token: "a-github-token-for-this-test",
      }),
    );
    // No navigation at all: no sign-in screen and no account chooser.
    expect(assign).not.toHaveBeenCalled();
    expect(post).not.toHaveBeenCalled();
  });

  it("will not submit a value too short to be a token", async () => {
    get.mockResolvedValue({ ...CONNECTED, connected: false, login: null, credential_kind: null });

    renderWithQuery(<GitHubConnection />);
    await userEvent.type(await screen.findByTestId("github-token-input"), "short");

    expect(screen.getByTestId("github-token-submit")).toBeDisabled();
    expect(put).not.toHaveBeenCalled();
  });

  it("keeps the GitHub App route as a labelled alternative when configured", async () => {
    get.mockResolvedValue({ ...CONNECTED, connected: false, login: null, credential_kind: null });
    post.mockResolvedValue({ authorize_url: "https://github.test/login", expires_in_seconds: 600 });
    const assign = vi.fn();
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { ...window.location, assign },
    });

    renderWithQuery(<GitHubConnection />);
    const alternative = await screen.findByTestId("github-oauth-alternative");
    // It says what it does before it does it, because the whole reason the token path is first is that
    // this one shows GitHub's sign-in and account-selection screens.
    expect(alternative).toHaveTextContent("opens github.com");
    expect(alternative).toHaveTextContent("choose which account");
    await userEvent.click(screen.getByTestId("github-connect"));

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
    del.mockResolvedValue({
      login: "octo-cat",
      revoked_at_github: true,
      credential_kind: "oauth_app",
    });

    renderWithQuery(<GitHubConnection />);
    await userEvent.click(await screen.findByTestId("github-disconnect"));

    await waitFor(() => expect(del).toHaveBeenCalledWith("/integrations/github"));
    expect(screen.queryByTestId("github-link-problem")).not.toBeInTheDocument();
  });

  it("says so when GitHub did not confirm the revocation", async () => {
    get.mockResolvedValue(CONNECTED);
    del.mockResolvedValue({
      login: "octo-cat",
      revoked_at_github: false,
      credential_kind: "oauth_app",
    });

    renderWithQuery(<GitHubConnection />);
    await userEvent.click(await screen.findByTestId("github-disconnect"));

    const problem = await screen.findByTestId("github-link-problem");
    expect(problem).toHaveTextContent("did not confirm");
    expect(problem).toHaveTextContent("GitHub");
  });

  it("tells the user where to delete a pasted token, rather than implying a failure", async () => {
    get.mockResolvedValue({ ...CONNECTED, credential_kind: "personal_token" });
    del.mockResolvedValue({
      login: "octo-cat",
      revoked_at_github: false,
      credential_kind: "personal_token",
    });

    renderWithQuery(<GitHubConnection />);
    await userEvent.click(await screen.findByTestId("github-disconnect"));

    const problem = await screen.findByTestId("github-link-problem");
    expect(problem).toHaveTextContent("still exists on GitHub");
    expect(problem).toHaveTextContent("Developer settings");
    // It must NOT read as "we tried and failed", which is what the other branch says: this server
    // cannot delete a token the person created, and saying it failed would send them looking for a bug.
    expect(problem).not.toHaveTextContent("did not confirm");
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
