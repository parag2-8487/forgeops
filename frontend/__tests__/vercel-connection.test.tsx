// SPDX-License-Identifier: FSL-1.1-ALv2
/**
 * Unit tests for VercelConnection integration component.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { VercelConnection, type VercelLinkStatus } from "@/features/integrations/VercelConnection";
import { ApiProblemError } from "@/lib/api";

const get = vi.fn();
const put = vi.fn();
const post = vi.fn();
const del = vi.fn();

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      get: (...args: unknown[]) => get(...args),
      put: (...args: unknown[]) => put(...args),
      post: (...args: unknown[]) => post(...args),
      delete: (...args: unknown[]) => del(...args),
    },
  };
});

const DISCONNECTED: VercelLinkStatus = {
  configured: false,
  connected: false,
  username: null,
  email: null,
  token_hint: null,
  last_tested_at: null,
  last_test_ok: null,
  last_test_detail: "",
};

const CONNECTED: VercelLinkStatus = {
  configured: true,
  connected: true,
  username: "parag-tester",
  email: "parag@example.com",
  token_hint: "4xyz",
  last_tested_at: "2026-10-08T20:00:00Z",
  last_test_ok: true,
  last_test_detail: "connected as parag-tester",
};

function renderWithQuery(element: ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={client}>{element}</QueryClientProvider>);
}

beforeEach(() => {
  get.mockReset();
  put.mockReset();
  post.mockReset();
  del.mockReset();
});

describe("VercelConnection", () => {
  it("renders disconnected state when not linked", async () => {
    get.mockResolvedValue(DISCONNECTED);

    renderWithQuery(<VercelConnection />);

    expect(await screen.findByTestId("vercel-link-disconnected")).toBeInTheDocument();
    expect(screen.getByTestId("vercel-token-form")).toBeInTheDocument();
    expect(screen.getByTestId("vercel-token-submit")).toBeDisabled();
  });

  it("submits a valid token to the integration endpoint", async () => {
    get.mockResolvedValue(DISCONNECTED);
    put.mockResolvedValue(CONNECTED);

    renderWithQuery(<VercelConnection />);

    const input = await screen.findByTestId("vercel-token-input");
    await userEvent.type(input, "vcp_valid_token_12345");

    const submit = screen.getByTestId("vercel-token-submit");
    expect(submit).not.toBeDisabled();
    await userEvent.click(submit);

    await waitFor(() => {
      expect(put).toHaveBeenCalledWith("/integrations/vercel/token", {
        token: "vcp_valid_token_12345",
      });
    });
  });

  it("surfaces API errors when token submission fails", async () => {
    get.mockResolvedValue(DISCONNECTED);
    put.mockRejectedValue(
      new ApiProblemError({
        type: "https://forgeops.dev/problems/vercel-deploy-failed",
        title: "Vercel Deploy Failed",
        status: 400,
        detail: "Vercel refused token: invalid credentials",
      }),
    );

    renderWithQuery(<VercelConnection />);

    const input = await screen.findByTestId("vercel-token-input");
    await userEvent.type(input, "vcp_invalid_token_9999");
    await userEvent.click(screen.getByTestId("vercel-token-submit"));

    expect(await screen.findByTestId("vercel-link-problem")).toHaveTextContent(
      "Vercel refused token: invalid credentials",
    );
  });

  it("renders connected state with account info and token hint", async () => {
    get.mockResolvedValue(CONNECTED);

    renderWithQuery(<VercelConnection />);

    expect(await screen.findByTestId("vercel-link-connected")).toBeInTheDocument();
    expect(screen.getByTestId("vercel-account-name")).toHaveTextContent("@parag-tester");
    expect(screen.getByTestId("vercel-token-hint")).toHaveTextContent("••••••••4xyz");
    expect(screen.getByTestId("vercel-disconnect")).toBeInTheDocument();
  });

  it("disconnects when disconnect button is clicked", async () => {
    get.mockResolvedValue(CONNECTED);
    del.mockResolvedValue({ connected: false });

    renderWithQuery(<VercelConnection />);

    const disconnectBtn = await screen.findByTestId("vercel-disconnect");
    await userEvent.click(disconnectBtn);

    await waitFor(() => {
      expect(del).toHaveBeenCalledWith("/integrations/vercel");
    });
  });

  it("surfaces errors when disconnect fails", async () => {
    get.mockResolvedValue(CONNECTED);
    del.mockRejectedValue(new Error("Network disconnect error"));

    renderWithQuery(<VercelConnection />);

    const disconnectBtn = await screen.findByTestId("vercel-disconnect");
    await userEvent.click(disconnectBtn);

    expect(await screen.findByTestId("vercel-link-problem")).toHaveTextContent(
      "Network disconnect error",
    );
  });

  it("tests connection successfully when test connection button is clicked", async () => {
    get.mockResolvedValue(CONNECTED);
    post.mockResolvedValue({
      ...CONNECTED,
      username: "parag-tester",
    });

    renderWithQuery(<VercelConnection />);

    const testBtn = await screen.findByTestId("vercel-test-connection");
    await userEvent.click(testBtn);

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/integrations/vercel/test");
    });
    expect(await screen.findByTestId("vercel-test-success")).toHaveTextContent(
      "Connection verified: connected as @parag-tester",
    );
  });

  it("surfaces error when test connection fails", async () => {
    get.mockResolvedValue(CONNECTED);
    post.mockRejectedValue(
      new ApiProblemError({
        type: "https://forgeops.dev/problems/vercel-deploy-failed",
        title: "Test Connection Failed",
        status: 401,
        detail: "Vercel refused token: token expired",
      }),
    );

    renderWithQuery(<VercelConnection />);

    const testBtn = await screen.findByTestId("vercel-test-connection");
    await userEvent.click(testBtn);

    expect(await screen.findByTestId("vercel-link-problem")).toHaveTextContent(
      "Vercel refused token: token expired",
    );
  });
});
