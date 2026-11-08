// SPDX-License-Identifier: FSL-1.1-ALv2
/** The Gemini section tests whichever Gemini model is chosen, fastest-measured first. */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import ModelTiersPage from "@/app/(shell)/models/page";

const { mockGet, mockPost } = vi.hoisted(() => ({ mockGet: vi.fn(), mockPost: vi.fn() }));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, api: { ...actual.api, get: mockGet, post: mockPost } };
});

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
  mockGet.mockImplementation((path: string) =>
    Promise.resolve(
      path === "/ai/tiers"
        ? { tiers: [] }
        : [
            {
              key_ref: "google",
              length: 39,
              hint: "abcd",
              last_tested_at: null,
              last_test_ok: null,
              last_test_detail: "",
            },
          ],
    ),
  );
  mockPost.mockResolvedValue({
    endpoint_id: "x",
    ok: true,
    detail: "connected",
    model_reported: null,
    latency_ms: 5,
    category: "connected",
  });
});

function renderPage() {
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <ModelTiersPage />
    </QueryClientProvider>,
  );
}

describe("the Gemini section", () => {
  it("defaults to the fastest measured model and tests it", async () => {
    renderPage();
    expect(screen.getByTestId("gemini-test-model")).toHaveValue("gemini-2.5-flash");
    await userEvent.click(await screen.findByTestId("cred-test-google"));
    await waitFor(() =>
      expect(mockPost).toHaveBeenCalledWith("/ai/endpoints/gemini-2.5-flash/test"),
    );
    expect(await screen.findByTestId("cred-test-category-google")).toHaveTextContent(
      /Connected successfully/,
    );
  });

  it("tests the model the operator picks, and drops the previous model's result", async () => {
    renderPage();
    await userEvent.click(await screen.findByTestId("cred-test-google"));
    await screen.findByTestId("cred-test-category-google");
    await userEvent.selectOptions(screen.getByTestId("gemini-test-model"), "gemini-3.8-flash");
    expect(screen.queryByTestId("cred-test-category-google")).toBeNull();
    await userEvent.click(screen.getByTestId("cred-test-google"));
    await waitFor(() =>
      expect(mockPost).toHaveBeenLastCalledWith("/ai/endpoints/gemini-3.8-flash/test"),
    );
  });
});
