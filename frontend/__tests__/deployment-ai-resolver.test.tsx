// SPDX-License-Identifier: FSL-1.1-ALv2
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { DeploymentAiResolver } from "@/features/deployments/DeploymentAiResolver";
import { ApiProblemError } from "@/lib/api";

const stream = vi.fn();
const post = vi.fn();

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      stream: (...args: unknown[]) => stream(...args),
      post: (...args: unknown[]) => post(...args),
      get: vi.fn(),
      put: vi.fn(),
      delete: vi.fn(),
    },
  };
});

describe("DeploymentAiResolver", () => {
  const projectId = "11111111-1111-4111-8111-111111111111";
  const deploymentId = "22222222-2222-4222-8222-222222222222";
  const manifests = ["Dockerfile", "docker-compose.yml"];

  beforeEach(() => {
    vi.clearAllMocks();
  });

  function mockSseResponse(events: Array<{ event: string; data?: any }>) {
    const response = {
      ok: true,
      status: 200,
      headers: new Headers({ "content-type": "text/event-stream" }),
      body: {
        getReader: () => {
          let done = false;
          return {
            read: async () => {
              if (done) return { done: true, value: undefined };
              done = true;
              const text = events
                .map((e) => `event: ${e.event}\ndata: ${JSON.stringify(e.data ?? {})}\n\n`)
                .join("");
              return { done: false, value: new TextEncoder().encode(text) };
            },
          };
        },
      },
    };
    stream.mockResolvedValue(response);
  }

  it("renders the initial idle state with action buttons", () => {
    const onRetryDeploy = vi.fn();
    render(
      <DeploymentAiResolver
        projectId={projectId}
        deploymentId={deploymentId}
        manifests={manifests}
        errorMessage="exit code: 127: vite: not found"
        onRetryDeploy={onRetryDeploy}
      />,
    );

    expect(screen.getByRole("button", { name: /resolve errors with ai/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /retry deploy/i })).toBeInTheDocument();
  });

  it("handles AI resolution streaming and successful acceptance", async () => {
    const user = userEvent.setup();
    mockSseResponse([
      { event: "token", data: { text: "Fixing builder stage..." } },
      {
        event: "complete",
        data: {
          files: ["Dockerfile"],
          change_set_id: "33333333-3333-4333-8333-333333333333",
        },
      },
    ]);

    render(
      <DeploymentAiResolver
        projectId={projectId}
        deploymentId={deploymentId}
        manifests={manifests}
        errorMessage="exit code: 127: vite: not found"
      />,
    );

    await user.click(screen.getByRole("button", { name: /resolve errors with ai/i }));

    expect(stream).toHaveBeenCalledWith(
      "/generation/runs",
      expect.objectContaining({
        method: "POST",
      }),
    );

    const callArgs = stream.mock.calls[0][1];
    const parsedBody = JSON.parse(callArgs.body);
    expect(parsedBody.prompt).toContain("BUILD_TOOL_MISSING");

    await waitFor(() => {
      expect(screen.getByText(/fix ready/i)).toBeInTheDocument();
      expect(screen.getByText(/corrected configuration generated/i)).toBeInTheDocument();
    });
  });

  it("classifies diverse error signatures and bounds error excerpt length", async () => {
    const user = userEvent.setup();
    mockSseResponse([
      {
        event: "complete",
        data: { files: ["Dockerfile"], change_set_id: "cs-1" },
      },
    ]);

    const longLog =
      "X".repeat(10_000) +
      "\nerror TS2307: cannot find module 'foo'\npermission denied\nport is already allocated\nno space left\nfailed to solve\nserver.js not found\nunsupported version\ntimeout tls handshake\nerr_pnpm\nexit code 127";
    render(
      <DeploymentAiResolver
        projectId={projectId}
        deploymentId={deploymentId}
        manifests={manifests}
        errorMessage={longLog}
      />,
    );

    await user.click(screen.getByRole("button", { name: /resolve errors with ai/i }));

    const callArgs = stream.mock.calls[0][1];
    const parsedBody = JSON.parse(callArgs.body);
    expect(parsedBody.prompt).toContain("earlier characters omitted");
    expect(parsedBody.prompt).toContain("TYPESCRIPT_COMPILE_ERROR");
    expect(parsedBody.prompt).toContain("PERMISSION_ERROR");
    expect(parsedBody.prompt).toContain("PORT_CONFLICT");
    expect(parsedBody.prompt).toContain("RESOURCE_EXHAUSTION");
    expect(parsedBody.prompt).toContain("DOCKERFILE_SYNTAX_ERROR");
    expect(parsedBody.prompt).toContain("WRONG_ENTRYPOINT");
    expect(parsedBody.prompt).toContain("RUNTIME_VERSION_MISMATCH");
    expect(parsedBody.prompt).toContain("NETWORK_TIMEOUT");
    expect(parsedBody.prompt).toContain("PACKAGE_MANAGER_ERROR");
    expect(parsedBody.prompt).toContain("BUILD_TOOL_MISSING");
  });

  it("handles stream failure events and retry action", async () => {
    const user = userEvent.setup();
    mockSseResponse([{ event: "error", data: { detail: "Model capacity reached" } }]);

    render(
      <DeploymentAiResolver
        projectId={projectId}
        deploymentId={deploymentId}
        manifests={manifests}
        errorMessage="unknown error"
      />,
    );

    await user.click(screen.getByRole("button", { name: /resolve errors with ai/i }));

    await waitFor(() => {
      expect(screen.getByText(/model capacity reached/i)).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /try again/i })).toBeInTheDocument();
    });

    // Test Try Again button
    mockSseResponse([
      { event: "complete", data: { files: ["Dockerfile"], change_set_id: "cs-try-again" } },
    ]);
    await user.click(screen.getByRole("button", { name: /try again/i }));

    await waitFor(() => {
      expect(screen.getByText(/fix ready/i)).toBeInTheDocument();
    });
  });

  it("handles missing terminal events as failures", async () => {
    const user = userEvent.setup();
    mockSseResponse([{ event: "token", data: { text: "Thinking..." } }]);

    render(
      <DeploymentAiResolver
        projectId={projectId}
        deploymentId={deploymentId}
        manifests={manifests}
        errorMessage="error"
      />,
    );

    await user.click(screen.getByRole("button", { name: /resolve errors with ai/i }));

    await waitFor(() => {
      expect(screen.getByText(/closed without a terminal event/i)).toBeInTheDocument();
    });
  });

  it("handles network ApiProblemError on request", async () => {
    const user = userEvent.setup();
    stream.mockRejectedValue(
      new ApiProblemError(
        { title: "Service Unavailable", detail: "Provider down", status: 503 },
        503,
      ),
    );

    render(
      <DeploymentAiResolver
        projectId={projectId}
        deploymentId={deploymentId}
        manifests={manifests}
        errorMessage="error"
      />,
    );

    await user.click(screen.getByRole("button", { name: /resolve errors with ai/i }));

    await waitFor(() => {
      expect(screen.getByText(/provider down/i)).toBeInTheDocument();
    });
  });

  it("approves the change set and handles version conflict gracefully", async () => {
    const user = userEvent.setup();
    mockSseResponse([
      {
        event: "complete",
        data: {
          files: ["Dockerfile"],
          change_set_id: "cs-approval-test",
        },
      },
    ]);
    // Simulate error during approveFix (e.g. version mismatch) which catches and still marks approved
    post.mockRejectedValue(new Error("Version conflict: already applied"));

    render(
      <DeploymentAiResolver
        projectId={projectId}
        deploymentId={deploymentId}
        manifests={manifests}
        errorMessage="error"
      />,
    );

    await user.click(screen.getByRole("button", { name: /resolve errors with ai/i }));

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /approve & apply to disk/i })).toBeInTheDocument();
    });

    await user.click(screen.getByRole("button", { name: /approve & apply to disk/i }));

    await waitFor(() => {
      expect(screen.getByText(/applied to disk!/i)).toBeInTheDocument();
    });
  });

  it("redeploy triggers approval and handles onRetryDeploy error", async () => {
    const user = userEvent.setup();
    const onRetryDeploy = vi.fn().mockRejectedValue(new Error("K8s cluster unreachable"));
    post.mockResolvedValue({ status: "approved" });

    mockSseResponse([
      {
        event: "complete",
        data: {
          files: ["Dockerfile"],
          change_set_id: "cs-redeploy-test",
        },
      },
    ]);

    render(
      <DeploymentAiResolver
        projectId={projectId}
        deploymentId={deploymentId}
        manifests={manifests}
        errorMessage="error"
        onRetryDeploy={onRetryDeploy}
      />,
    );

    await user.click(screen.getByRole("button", { name: /resolve errors with ai/i }));

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /re-deploy now/i })).toBeInTheDocument();
    });

    await user.click(screen.getByRole("button", { name: /re-deploy now/i }));

    await waitFor(() => {
      expect(screen.getByText(/k8s cluster unreachable/i)).toBeInTheDocument();
    });
  });
});
