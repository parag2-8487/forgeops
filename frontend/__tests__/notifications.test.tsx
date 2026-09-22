// SPDX-License-Identifier: FSL-1.1-ALv2
/**
 * §2.6's bell and preferences.
 *
 * Two properties. Per-channel delivery is visible, so a notification rejected by Slack cannot look
 * delivered — and "delivered nowhere" is its own sentence. And a webhook URL is never displayed: the screen
 * shows whether one is set, because anybody holding it can post into the channel.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  deliverySentence,
  type Notification,
  NotificationBell,
  NotificationPreferences,
} from "@/features/notifications/NotificationBell";

const get = vi.fn();
const post = vi.fn();
const put = vi.fn();

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      get: (...args: unknown[]) => get(...args),
      post: (...args: unknown[]) => post(...args),
      put: (...args: unknown[]) => put(...args),
      delete: vi.fn(),
      patch: vi.fn(),
    },
  };
});

function mount(element: ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={client}>{element}</QueryClientProvider>);
}

function notification(overrides: Partial<Notification> = {}): Notification {
  return {
    id: "n1",
    kind: "deploy_failed",
    subject: "demo: deployment to production did not converge",
    body: "checkout-api never became ready",
    resource_kind: "deployment",
    resource_id: "d1",
    delivery: { in_app: { delivered: true, detail: "the row is the delivery" } },
    read_at: null,
    created_at: "2026-09-22T08:00:00Z",
    ...overrides,
  };
}

beforeEach(() => {
  get.mockReset();
  post.mockReset();
  put.mockReset();
});

describe("the delivery sentence", () => {
  it("distinguishes delivered, partly delivered and delivered nowhere", () => {
    expect(deliverySentence({})).toContain("only recorded here");
    expect(deliverySentence({ slack: { delivered: true, detail: "accepted" } })).toBe(
      "delivered to slack",
    );
    // THE ONE THAT MATTERS: a failure must not read as a success.
    expect(deliverySentence({ slack: { delivered: false, detail: "500" } })).toContain(
      "delivered nowhere",
    );
    const mixed = deliverySentence({
      in_app: { delivered: true, detail: "" },
      slack: { delivered: false, detail: "500" },
    });
    expect(mixed).toContain("delivered to in_app");
    expect(mixed).toContain("slack failed");
  });
});

describe("the bell", () => {
  it("shows the count as unknown while loading rather than as zero", async () => {
    get.mockReturnValue(new Promise(() => {}));
    mount(<NotificationBell projectId="p1" />);
    // A badge reading 0 on a project with unread notifications is worse than no badge.
    expect(await screen.findByTestId("bell-count-unknown")).toBeInTheDocument();
    expect(screen.queryByTestId("bell-count")).not.toBeInTheDocument();
  });

  it("shows the unread count when it is known", async () => {
    get.mockResolvedValue({ notifications: [notification()], unread: 1, kinds: [] });
    mount(<NotificationBell projectId="p1" />);
    expect(await screen.findByTestId("bell-count")).toHaveTextContent("1 unread");
  });

  it("renders each notification with where it actually got to", async () => {
    get.mockResolvedValue({
      notifications: [
        notification({
          delivery: {
            in_app: { delivered: true, detail: "" },
            slack: { delivered: false, detail: "the webhook returned 500" },
          },
        }),
      ],
      unread: 1,
      kinds: [],
    });
    mount(<NotificationBell projectId="p1" />);
    await userEvent.click(await screen.findByTestId("bell-toggle"));
    expect(screen.getByTestId("bell-delivery-n1")).toHaveTextContent("slack failed");
  });

  it("marks one read through the route", async () => {
    get.mockResolvedValue({ notifications: [notification()], unread: 1, kinds: [] });
    post.mockResolvedValue({ id: "n1", read_at: "2026-09-22T09:00:00Z" });
    mount(<NotificationBell projectId="p1" />);
    await userEvent.click(await screen.findByTestId("bell-toggle"));
    await userEvent.click(screen.getByTestId("bell-read-n1"));
    expect(post).toHaveBeenCalledWith("/projects/p1/notifications/n1/read", {});
  });

  it("states an empty list rather than leaving the dropdown blank", async () => {
    get.mockResolvedValue({ notifications: [], unread: 0, kinds: [] });
    mount(<NotificationBell projectId="p1" />);
    await userEvent.click(await screen.findByTestId("bell-toggle"));
    expect(screen.getByTestId("bell-empty")).toHaveTextContent("Nothing has been raised");
  });
});

describe("the preferences", () => {
  it("never shows a target, only whether one is set", async () => {
    const secret = "https://hooks.slack.test/T000/B000/zzzzzzzz";
    get.mockResolvedValue({
      preferences: [
        { channel: "slack", kind: "deploy_failed", enabled: true, target_configured: true },
      ],
      channels: ["in_app", "slack"],
      kinds: ["deploy_failed"],
    });
    const view = mount(<NotificationPreferences projectId="p1" />);
    expect(await screen.findByTestId("pref-target-slack-deploy_failed")).toHaveTextContent(
      "target configured",
    );
    // Asserted against the rendered DOM, so a field added later that carried the URL would fail here.
    expect(view.container.textContent).not.toContain(secret);
    expect(screen.getByTestId("preferences-credential-note")).toHaveTextContent(
      "never shown again",
    );
  });

  it("refuses to enable a delivering channel with nowhere to deliver", async () => {
    get.mockResolvedValue({
      preferences: [],
      channels: ["in_app", "slack"],
      kinds: ["deploy_failed"],
    });
    mount(<NotificationPreferences projectId="p1" />);
    await screen.findByTestId("pref-channel");
    // Slack is the default channel and the target is empty, so the button is disabled and the reason is
    // shown BEFORE the attempt rather than as a refusal after it.
    expect(screen.getByTestId("pref-save")).toBeDisabled();
    expect(screen.getByTestId("pref-target-required")).toHaveTextContent(
      "needs somewhere to deliver",
    );
    expect(put).not.toHaveBeenCalled();
  });

  it("saves a channel with a target", async () => {
    get.mockResolvedValue({
      preferences: [],
      channels: ["in_app", "slack"],
      kinds: ["deploy_failed"],
    });
    put.mockResolvedValue({ channel: "slack", kind: "deploy_failed", enabled: true });
    mount(<NotificationPreferences projectId="p1" />);
    await userEvent.type(await screen.findByTestId("pref-target"), "https://hooks.slack.test/abc");
    await userEvent.click(screen.getByTestId("pref-save"));
    expect(put).toHaveBeenCalledWith("/projects/p1/notifications/preferences", {
      channel: "slack",
      kind: "deploy_failed",
      enabled: true,
      target: "https://hooks.slack.test/abc",
    });
  });

  it("says plainly when no channel is configured", async () => {
    get.mockResolvedValue({ preferences: [], channels: ["in_app"], kinds: ["deploy_failed"] });
    mount(<NotificationPreferences projectId="p1" />);
    expect(await screen.findByTestId("pref-list")).toHaveTextContent("recorded here only");
  });
});
