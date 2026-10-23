// SPDX-License-Identifier: FSL-1.1-ALv2
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  RepositoryPicker,
  type RepositoryItem,
  type RepositoryPage,
} from "@/features/integrations/RepositoryPicker";
import { ApiProblemError } from "@/lib/api";

const get = vi.fn();

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      get: (...args: unknown[]) => get(...args),
      post: vi.fn(),
      put: vi.fn(),
      delete: vi.fn(),
    },
  };
});

describe("RepositoryPicker", () => {
  function renderWithClient(ui: React.ReactElement) {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    return render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>);
  }

  const sampleRepo: RepositoryItem = {
    full_name: "acme/web-app",
    owner: "acme",
    name: "web-app",
    private: true,
    default_branch: "main",
    language: "TypeScript",
    pushed_at: "2026-10-05T12:00:00Z",
    clone_url: "https://github.com/acme/web-app.git",
    html_url: "https://github.com/acme/web-app",
    size_kb: 450,
    archived: false,
  };

  const sampleArchived: RepositoryItem = {
    full_name: "acme/old-api",
    owner: "acme",
    name: "old-api",
    private: false,
    default_branch: "master",
    language: "",
    pushed_at: "",
    clone_url: "https://github.com/acme/old-api.git",
    html_url: "https://github.com/acme/old-api",
    size_kb: 100,
    archived: true,
  };

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders loading state initially", () => {
    get.mockReturnValue(new Promise(() => {}));
    renderWithClient(<RepositoryPicker selected={null} onSelect={vi.fn()} />);

    expect(screen.getByTestId("repo-picker-loading")).toBeInTheDocument();
  });

  it("renders unlinked state when error has github-link-absent", async () => {
    get.mockRejectedValue(
      new ApiProblemError({
        type: "https://forgeops.dev/problems/github-link-absent",
        title: "GitHub Not Linked",
        detail: "Connect a GitHub account to choose repositories.",
        status: 400,
      }),
    );

    renderWithClient(<RepositoryPicker selected={null} onSelect={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByTestId("repo-picker-unlinked")).toBeInTheDocument();
      expect(
        screen.getByText(/connect a github account to choose repositories/i),
      ).toBeInTheDocument();
    });
  });

  it("renders generic error state on other API errors", async () => {
    get.mockRejectedValue(new Error("Network failed"));

    renderWithClient(<RepositoryPicker selected={null} onSelect={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByTestId("repo-picker-error")).toBeInTheDocument();
      expect(screen.getByText(/network failed/i)).toBeInTheDocument();
    });
  });

  it("renders malformed response error when items is not an array", async () => {
    get.mockResolvedValue({});

    renderWithClient(<RepositoryPicker selected={null} onSelect={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByTestId("repo-picker-error")).toBeInTheDocument();
      expect(screen.getByText(/server did not return a repository page/i)).toBeInTheDocument();
    });
  });

  it("renders empty list state with guidance", async () => {
    get.mockResolvedValue({
      items: [],
      total: 0,
      page: 1,
      per_page: 10,
      truncated: false,
    });

    renderWithClient(<RepositoryPicker selected={null} onSelect={vi.fn()} />);

    await waitFor(() => {
      expect(
        screen.getByText(/your linked github account can reach no repositories/i),
      ).toBeInTheDocument();
    });
  });

  it("renders repositories and selects an item", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    const page: RepositoryPage = {
      items: [sampleRepo, sampleArchived],
      total: 2,
      page: 1,
      per_page: 10,
      truncated: false,
    };
    get.mockResolvedValue(page);

    renderWithClient(<RepositoryPicker selected={null} onSelect={onSelect} />);

    await waitFor(() => {
      expect(screen.getByText("acme/web-app")).toBeInTheDocument();
      expect(screen.getByText("acme/old-api")).toBeInTheDocument();
    });

    expect(screen.getByText(/private/i)).toBeInTheDocument();
    expect(screen.getByText(/TypeScript/i)).toBeInTheDocument();
    expect(screen.getByText(/no language reported/i)).toBeInTheDocument();
    expect(screen.getByText(/never pushed/i)).toBeInTheDocument();
    expect(screen.getByText(/archived/i)).toBeInTheDocument();

    await user.click(screen.getByTestId("repo-option-acme/web-app"));
    expect(onSelect).toHaveBeenCalledWith(sampleRepo);
  });

  it("handles searching and pagination controls", async () => {
    const user = userEvent.setup();
    let currentPage = 1;
    get.mockImplementation((url: string) => {
      const match = url.match(/page=(\d+)/);
      if (match) {
        currentPage = parseInt(match[1], 10);
      }
      return Promise.resolve({
        items: [sampleRepo],
        total: 25,
        page: currentPage,
        per_page: 10,
        truncated: true,
      });
    });

    renderWithClient(<RepositoryPicker selected={sampleRepo} onSelect={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText(/first 1,000 repositories/i)).toBeInTheDocument();
      expect(screen.getByTestId("repo-picker-page")).toHaveTextContent("Page 1 of 3");
    });

    // Check Previous is disabled on page 1
    expect(screen.getByTestId("repo-picker-prev")).toBeDisabled();
    expect(screen.getByTestId("repo-picker-next")).not.toBeDisabled();

    // Next page click
    await user.click(screen.getByTestId("repo-picker-next"));
    await waitFor(() => {
      expect(screen.getByTestId("repo-picker-page")).toHaveTextContent("Page 2 of 3");
    });

    // Previous page click
    await user.click(screen.getByTestId("repo-picker-prev"));
    await waitFor(() => {
      expect(screen.getByTestId("repo-picker-page")).toHaveTextContent("Page 1 of 3");
    });

    // Search query input
    const searchInput = screen.getByTestId("repo-picker-search");
    fireEvent.change(searchInput, { target: { value: "web" } });

    await waitFor(() => {
      expect(get).toHaveBeenCalledWith(expect.stringContaining("query=web"));
    });
  });
});
