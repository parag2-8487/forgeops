// SPDX-License-Identifier: FSL-1.1-ALv2

/**
 * The cache-correctness properties of the query-key factory, and the two modules that shipped with
 * no test at all.
 *
 * WHY QUERY KEYS ARE WORTH TESTING. A key factory looks like data, so it looks untestable. But
 * `query-keys.ts` carries two decisions whose comments state a CONSEQUENCE, and a consequence can be
 * asserted: the project list includes its limit because "two pages of different sizes are different
 * responses, and sharing a key would serve one from the other's cache entry", and the secret list
 * includes its project because a key ignoring it "would serve one project's references from
 * another's cache entry". The second is a cross-tenant read. Both reduce to a property — distinct
 * arguments must produce distinct keys, and every key must remain prefixed by its family so
 * invalidating the family reaches it — and both are tested here rather than assumed.
 *
 * The factory functions were 62.5% covered, which for a module of nothing but functions means a
 * third of them had never been called.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { queryKeys } from "@/lib/api/query-keys";

describe("queryKeys", () => {
  it("prefixes every key with its family, so invalidating the family reaches all of it", () => {
    // TanStack Query invalidates by key PREFIX. If a specific key did not start with its family
    // array, `invalidateQueries({ queryKey: queryKeys.projects.all })` would silently miss it and
    // the UI would keep showing a stale value after a mutation.
    // The keys are `as const` tuples, so the annotation has to be readonly all the way down;
    // `unknown[][]` would silently require mutable arrays the factory never produces.
    const families: Array<[readonly string[], ReadonlyArray<readonly unknown[]>]> = [
      [queryKeys.health.all, [queryKeys.health.status(), queryKeys.health.ready()]],
      [queryKeys.mcp.all, [queryKeys.mcp.servers(), queryKeys.mcp.tools("srv")]],
      [queryKeys.ai.all, [queryKeys.ai.tiers()]],
      [
        queryKeys.projects.all,
        [
          queryKeys.projects.list(20),
          queryKeys.projects.detail("p1"),
          queryKeys.projects.activity("p1"),
          queryKeys.projects.readiness("p1"),
        ],
      ],
      [queryKeys.audit.all, [queryKeys.audit.events(50)]],
      [queryKeys.policies.all, [queryKeys.policies.templates()]],
      [
        queryKeys.approvals.all,
        [queryKeys.approvals.list("pending"), queryKeys.approvals.detail("a1")],
      ],
      [queryKeys.devices.all, [queryKeys.devices.list(), queryKeys.devices.detail("d1")]],
      [queryKeys.secrets.all, [queryKeys.secrets.list("p1")]],
    ];

    for (const [family, members] of families) {
      expect(members.length).toBeGreaterThan(0);
      for (const key of members) {
        expect(key.slice(0, family.length)).toEqual([...family]);
      }
    }
  });

  it("gives two page sizes different keys, so one is not served from the other's entry", () => {
    expect(queryKeys.projects.list(20)).not.toEqual(queryKeys.projects.list(50));
    expect(queryKeys.audit.events(20)).not.toEqual(queryKeys.audit.events(50));
  });

  it("gives two projects different secret keys — sharing one would be a cross-tenant read", () => {
    // The sharpest of these properties. `GET /api/v1/secrets` requires `project_id`, so a key that
    // ignored it would let project B's operator see project A's cached secret references.
    expect(queryKeys.secrets.list("project-a")).not.toEqual(queryKeys.secrets.list("project-b"));
  });

  it("distinguishes every other parameterised key by its argument", () => {
    expect(queryKeys.mcp.tools("alpha")).not.toEqual(queryKeys.mcp.tools("beta"));
    expect(queryKeys.projects.detail("a")).not.toEqual(queryKeys.projects.detail("b"));
    expect(queryKeys.projects.activity("a")).not.toEqual(queryKeys.projects.activity("b"));
    expect(queryKeys.projects.readiness("a")).not.toEqual(queryKeys.projects.readiness("b"));
    expect(queryKeys.approvals.list("pending")).not.toEqual(queryKeys.approvals.list("approved"));
    expect(queryKeys.approvals.detail("a")).not.toEqual(queryKeys.approvals.detail("b"));
    expect(queryKeys.devices.detail("a")).not.toEqual(queryKeys.devices.detail("b"));
  });

  it("keeps sibling reads on the same id apart, so one does not overwrite another", () => {
    // Same project, three different questions. If `detail` and `readiness` collided, opening a
    // project would replace its readiness scores with its metadata.
    const keys = [
      queryKeys.projects.detail("p1"),
      queryKeys.projects.activity("p1"),
      queryKeys.projects.readiness("p1"),
    ].map((k) => JSON.stringify(k));
    expect(new Set(keys).size).toBe(3);
  });

  it("keeps the families themselves distinct, so invalidating one does not clear another", () => {
    const all = [
      queryKeys.health.all,
      queryKeys.mcp.all,
      queryKeys.ai.all,
      queryKeys.projects.all,
      queryKeys.audit.all,
      queryKeys.policies.all,
      queryKeys.approvals.all,
      queryKeys.devices.all,
      queryKeys.secrets.all,
    ].map((k) => JSON.stringify(k));
    expect(new Set(all).size).toBe(all.length);
  });
});

describe("the public API surface", () => {
  it("re-exports exactly what consumers import, so the barrel cannot silently drop one", async () => {
    // `lib/api/index.ts` was 0% — nothing imported through the barrel in a test, so a deleted
    // re-export would have broken every page at build time with no test failing first.
    const barrel = await import("@/lib/api");
    expect(typeof barrel.api.get).toBe("function");
    expect(typeof barrel.api.post).toBe("function");
    expect(typeof barrel.api.put).toBe("function");
    expect(typeof barrel.api.delete).toBe("function");
    expect(typeof barrel.api.stream).toBe("function");
    expect(typeof barrel.isProblemDetails).toBe("function");
    expect(barrel.PROBLEM_CONTENT_TYPE).toBe("application/problem+json");
    expect(barrel.queryKeys.projects.all).toEqual(["projects"]);
    expect(new barrel.ApiProblemError({ type: "u", title: "t", status: 1 })).toBeInstanceOf(Error);
    expect(new barrel.ApiTransportError({ type: "u", title: "t", status: 0 })).toBeInstanceOf(
      barrel.ApiProblemError,
    );
  });
});

describe("ThemeProvider", () => {
  it("passes its configuration through and renders its children", async () => {
    // Eight lines that shipped at 0%. The delegation is the whole content of the module, so the
    // assertion is that the props reach `next-themes` and the tree still renders.
    const spy = vi.fn();
    vi.doMock("next-themes", () => ({
      ThemeProvider: (props: Record<string, unknown>) => {
        spy(props);
        return <div data-testid="themed">{props.children as React.ReactNode}</div>;
      },
    }));

    const { ThemeProvider } = await import("@/components/providers/theme-provider");
    render(
      <ThemeProvider attribute="class" defaultTheme="system">
        <span>content</span>
      </ThemeProvider>,
    );

    expect(screen.getByTestId("themed")).toBeInTheDocument();
    expect(screen.getByText("content")).toBeInTheDocument();
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({ attribute: "class", defaultTheme: "system" }),
    );
    vi.doUnmock("next-themes");
  });
});

describe("the shell layout", () => {
  it("offers a skip link FIRST, and guards only the panel rather than the chrome", async () => {
    // Two things the layout's own comment claims, neither previously asserted:
    //   1. a keyboard user can jump past the navigation, which needs the link to be the first
    //      focusable thing and to target an element that can actually receive focus;
    //   2. `AuthBoundary` sits INSIDE <main>, so the sidebar and header stay rendered while the
    //      session is restoring. Wrapping the whole shell would blank the chrome on every reload.
    vi.doMock("@/components/layout/app-sidebar", () => ({
      AppSidebar: () => <nav data-testid="sidebar">nav</nav>,
    }));
    vi.doMock("@/components/layout/app-header", () => ({
      AppHeader: () => <header data-testid="header">header</header>,
    }));
    vi.doMock("@/components/layout/auth-boundary", () => ({
      AuthBoundary: ({ children }: { children: React.ReactNode }) => (
        <div data-testid="boundary">{children}</div>
      ),
    }));

    const ShellLayout = (await import("@/app/(shell)/layout")).default;
    const { container } = render(<ShellLayout>{<p>panel</p>}</ShellLayout>);

    const skip = screen.getByRole("link", { name: /skip to main content/i });
    expect(skip).toHaveAttribute("href", "#main");

    // It must come before the navigation in DOM order, or it is not a skip link.
    const focusables = Array.from(container.querySelectorAll("a, nav, header, main"));
    expect(focusables[0]).toBe(skip);

    // Its target must be focusable, which for a non-interactive element means an explicit
    // tabindex. Without it the link moves the reading position but not the focus ring.
    const main = container.querySelector("main");
    expect(main).toHaveAttribute("id", "main");
    expect(main).toHaveAttribute("tabindex", "-1");

    // The boundary is inside main; the chrome is outside it.
    expect(main).toContainElement(screen.getByTestId("boundary"));
    expect(main).not.toContainElement(screen.getByTestId("sidebar"));
    expect(main).not.toContainElement(screen.getByTestId("header"));
    expect(screen.getByTestId("boundary")).toContainElement(screen.getByText("panel"));

    vi.doUnmock("@/components/layout/app-sidebar");
    vi.doUnmock("@/components/layout/app-header");
    vi.doUnmock("@/components/layout/auth-boundary");
  });
});
