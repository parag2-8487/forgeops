// SPDX-License-Identifier: FSL-1.1-ALv2
/**
 * The secret vault's write path — phases.md §1.8 "Frontend: Secret vault UI (add, edit, delete)".
 *
 * THE PROPERTY UNDER TEST IS STRUCTURAL, NOT BEHAVIOURAL.
 *
 * The requirement is that the UI never displays or caches a secret value it wrote, and that this be
 * structural rather than a comment. So the assertions are about the SHAPE of the form, not about what
 * happens to be on screen:
 *
 *  - the value input is UNCONTROLLED — no `value` attribute bound to state — so the string cannot
 *    appear in a React state snapshot, a DevTools inspection, or an error boundary;
 *  - the DOM node is cleared as part of the mutation, so a FAILED request clears it too;
 *  - the value is never rendered anywhere afterwards.
 *
 * A test that only checked "the value is not on screen after saving" would pass over an
 * implementation that held it in `useState` for the lifetime of the page, which is the actual risk.
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

vi.mock("next/navigation", () => ({ usePathname: () => "/vault" }));

import VaultPage from "@/app/(shell)/vault/page";

const SECRET = {
  id: "s-1",
  key: "DATABASE_PASSWORD",
  environment: "production",
  infisical_path: null,
  is_local: true,
};

const PROJECTS = {
  projects: [{ id: "11111111-1111-1111-1111-111111111111", name: "picker-fixture" }],
  next_cursor: null,
};

function renderPage(ui: React.ReactElement) {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0, staleTime: 0 },
      mutations: { retry: false },
    },
  });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

function serve(secrets: unknown[] = [SECRET]) {
  mockGet.mockImplementation((path: string) =>
    path.startsWith("/projects?limit=") ? Promise.resolve(PROJECTS) : Promise.resolve(secrets),
  );
}

beforeEach(() => {
  for (const m of [mockGet, mockPost, mockPatch, mockDelete]) m.mockReset();
});
afterEach(() => cleanup());

describe("the value field is structurally write-only", () => {
  it("is an uncontrolled password input with no React state behind it", async () => {
    serve([]);
    renderPage(<VaultPage />);
    const input = (await screen.findByLabelText("Value")) as HTMLInputElement;

    expect(input).toHaveAttribute("type", "password");
    // The load-bearing assertion. A controlled input renders a `value` attribute reflecting state; an
    // uncontrolled one does not, so its content exists only in the DOM node.
    expect(input.getAttribute("value")).toBeNull();
    // `new-password` so a password manager does not offer to fill — and does not offer to SAVE this
    // under the site's own credentials.
    expect(input).toHaveAttribute("autocomplete", "new-password");
  });

  it("warns that the value cannot be read back, before it is written", async () => {
    serve([]);
    renderPage(<VaultPage />);
    expect(await screen.findByText(/you will not be able to read it back/i)).toBeInTheDocument();
    expect(screen.getByText(/never enters application state/i)).toBeInTheDocument();
  });

  it("sends the value and clears the field, leaving it nowhere on the page", async () => {
    serve([]);
    mockPost.mockResolvedValue(SECRET);
    renderPage(<VaultPage />);

    const input = (await screen.findByLabelText("Value")) as HTMLInputElement;
    await userEvent.type(screen.getByLabelText("Key"), "DATABASE_PASSWORD");
    await userEvent.clear(screen.getByLabelText("Environment"));
    await userEvent.type(screen.getByLabelText("Environment"), "production");
    await userEvent.type(input, "s3cr3t-value");
    await userEvent.click(screen.getByTestId("create-secret"));

    await waitFor(() =>
      expect(mockPost).toHaveBeenCalledWith("/secrets", {
        project_id: "11111111-1111-1111-1111-111111111111",
        environment: "production",
        key: "DATABASE_PASSWORD",
        value: "s3cr3t-value",
      }),
    );

    // Cleared, and cleared BEFORE the request was awaited — so the material is out of the DOM while
    // the write is still in flight rather than after it returns.
    await waitFor(() => expect(input.value).toBe(""));
    expect(document.body.textContent).not.toContain("s3cr3t-value");
  });

  it("clears the field even when the write FAILS, rather than leaving material on the page", async () => {
    serve([]);
    const { ApiProblemError } = await import("@/lib/api");
    mockPost.mockRejectedValue(
      new ApiProblemError({
        type: "https://errors.forgeops.dev/secret-store-unavailable",
        title: "Secret store unavailable",
        status: 503,
      }),
    );
    renderPage(<VaultPage />);

    const input = (await screen.findByLabelText("Value")) as HTMLInputElement;
    await userEvent.type(screen.getByLabelText("Key"), "K");
    await userEvent.type(input, "s3cr3t-value");
    await userEvent.click(screen.getByTestId("create-secret"));

    await screen.findByTestId("governance-refusal");
    // Keeping it so the user could retry would hold live credential material in the DOM for as long as
    // the tab stayed open. Retyping a secret is cheap; that is not.
    expect(input.value).toBe("");
    expect(document.body.textContent).not.toContain("s3cr3t-value");
  });
});

describe("rotation", () => {
  it("patches the value, leaving the key and environment untouched", async () => {
    serve();
    mockPatch.mockResolvedValue(SECRET);
    renderPage(<VaultPage />);

    await userEvent.click(await screen.findByTestId("rotate-s-1"));
    await userEvent.type(screen.getByLabelText("New value"), "rotated-value");
    await userEvent.click(screen.getByTestId("confirm-rotate-s-1"));

    // A PATCH of the value rather than a delete-and-recreate, so nothing that injects this secret has
    // to be reconfigured. That is the whole point of rotation being its own operation.
    await waitFor(() =>
      expect(mockPatch).toHaveBeenCalledWith("/secrets/s-1", { value: "rotated-value" }),
    );
    expect(document.body.textContent).not.toContain("rotated-value");
  });

  it("is a disclosure the trigger reports the state of", async () => {
    serve();
    renderPage(<VaultPage />);
    const trigger = await screen.findByTestId("rotate-s-1");
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
  });
});

describe("deletion", () => {
  it("confirms first, and says what is NOT deleted", async () => {
    serve([{ ...SECRET, is_local: false, infisical_path: "/apps/api" }]);
    renderPage(<VaultPage />);

    await userEvent.click(await screen.findByTestId("delete-secret-s-1"));
    const dialog = screen.getByRole("alertdialog");
    expect(dialog).toHaveTextContent(/removes the metadata record/i);
    // The honest limit: this platform does not own Infisical, so reaching into it would be acting
    // outside what it manages. Saying so is the difference between a limitation and a surprise.
    expect(dialog).toHaveTextContent(/material in Infisical is not\s+removed/i);
  });

  it("deletes only after the confirmation is accepted", async () => {
    serve();
    mockDelete.mockResolvedValue(undefined);
    renderPage(<VaultPage />);

    await userEvent.click(await screen.findByTestId("delete-secret-s-1"));
    expect(mockDelete).not.toHaveBeenCalled();

    await userEvent.click(screen.getByTestId("confirm-delete-secret"));
    await waitFor(() => expect(mockDelete).toHaveBeenCalledWith("/secrets/s-1"));
  });

  it("abandons the deletion on cancel", async () => {
    serve();
    renderPage(<VaultPage />);
    await userEvent.click(await screen.findByTestId("delete-secret-s-1"));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(mockDelete).not.toHaveBeenCalled();
  });
});

describe("the page's own preconditions", () => {
  it("makes no secrets request before a project is chosen", async () => {
    // `project_id` is REQUIRED on this endpoint. The screen originally called it with no query string
    // at all, so every visit produced 422 and the panel reported it as an error loading references —
    // the request was malformed, not the response.
    mockGet.mockImplementation((path: string) =>
      path.startsWith("/projects?limit=")
        ? Promise.resolve({ projects: [], next_cursor: null })
        : Promise.reject(new Error("must not be called")),
    );
    renderPage(<VaultPage />);
    await waitFor(() => expect(mockGet).toHaveBeenCalled());
    expect(mockGet.mock.calls.every((c) => !String(c[0]).startsWith("/secrets"))).toBe(true);
  });

  it("keeps the add form available when there is nothing to list", async () => {
    serve([]);
    renderPage(<VaultPage />);
    // An empty state that replaced the control which fills it would be a dead end. This is why the
    // page's `AsyncState` has no `isEmpty` branch.
    expect(await screen.findByTestId("create-secret")).toBeInTheDocument();
    expect(screen.getByText(/none registered/i)).toBeInTheDocument();
  });
});
