import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// i.hoisted because i.mock's factory is lifted above the imports, so a plain const is not
// initialised when the factory runs.
const { mockGet } = vi.hoisted(() => ({ mockGet: vi.fn() }));
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, api: { ...actual.api, get: mockGet } };
});

import { AgentConnectPanel } from "@/features/agent/AgentConnectPanel";
import { CodeCountdown } from "@/features/agent/CodeCountdown";
import {
  AGENT_COMMANDS,
  archiveName,
  detectPlatform,
  installOnPathCommand,
  renderCommand,
} from "@/features/agent/commands";

/**
 * The printed command must run as printed.
 *
 * The Pairing screen used to print `forgeops-agent pair --code BYDPQC`, which fails three ways: on
 * Windows PowerShell does not search the current directory, the binary is `forgeops-agent.exe`, and
 * `pair` refuses without `--backend`. A separate rendered command once carried `scan --path`, a flag
 * the CLI has never accepted, and it was found by running it.
 */

describe("renderCommand", () => {
  it("prefixes .\\ for PowerShell, because PowerShell will not run a bare name from the current directory", () => {
    const command = renderCommand({
      verb: "pair",
      platform: "windows",
      shell: "powershell",
      location: { kind: "current-directory" },
      flags: { code: "BYDPQC" },
    });
    expect(command).toBe(".\\forgeops-agent.exe pair --code BYDPQC");
  });

  it("does not prefix for cmd.exe, which does search the current directory", () => {
    const command = renderCommand({
      verb: "pair",
      platform: "windows",
      shell: "cmd",
      location: { kind: "current-directory" },
      flags: { code: "BYDPQC" },
    });
    expect(command).toBe("forgeops-agent.exe pair --code BYDPQC");
  });

  it("prefixes ./ and drops .exe on POSIX", () => {
    const command = renderCommand({
      verb: "pair",
      platform: "linux",
      location: { kind: "current-directory" },
      flags: { code: "BYDPQC" },
    });
    expect(command).toBe("./forgeops-agent pair --code BYDPQC");
  });

  it("uses the bare name once the binary is on PATH, on every platform", () => {
    expect(
      renderCommand({ verb: "doctor", platform: "windows", location: { kind: "on-path" } }),
    ).toBe("forgeops-agent.exe doctor");
    expect(
      renderCommand({ verb: "doctor", platform: "macos", location: { kind: "on-path" } }),
    ).toBe("forgeops-agent doctor");
  });

  it("refuses a flag the CLI does not have", () => {
    // THE `scan --path` DEFECT, as a unit test. A component that asks for a flag that does not
    // exist now fails the test that renders it, rather than reaching a user.
    expect(() => renderCommand({ verb: "scan", platform: "linux", flags: { path: "." } })).toThrow(
      /has no --path flag/,
    );
  });

  it("names the flags a verb does have when it refuses", () => {
    expect(() =>
      renderCommand({ verb: "scan", platform: "linux", flags: { nonsense: "x" } }),
    ).toThrow(/project/);
  });

  it("omits a false boolean and renders a true one bare", () => {
    expect(renderCommand({ verb: "watch", platform: "linux", flags: { once: false } })).toBe(
      "forgeops-agent watch",
    );
    expect(renderCommand({ verb: "watch", platform: "linux", flags: { once: true } })).toBe(
      "forgeops-agent watch --once",
    );
  });

  it("quotes a path with a space, per shell", () => {
    // `C:\Users\Someone\My Project` is the realistic case, and unquoted it silently becomes two
    // arguments — a command that looks right and does the wrong thing.
    expect(
      renderCommand({
        verb: "connect",
        platform: "windows",
        shell: "powershell",
        flags: { code: "ABC234", workspace: "C:\\Users\\Someone\\My Project" },
      }),
    ).toContain('--workspace "C:\\Users\\Someone\\My Project"');

    expect(
      renderCommand({
        verb: "connect",
        platform: "linux",
        flags: { code: "ABC234", workspace: "/home/someone/my project" },
      }),
    ).toContain("--workspace '/home/someone/my project'");
  });

  it("renders a complete connect command with a backend, which is what pair refused without", () => {
    const command = renderCommand({
      verb: "connect",
      platform: "windows",
      shell: "powershell",
      flags: { code: "BYDPQC", backend: "ws://localhost:18000/api/v1/ws/agent" },
    });
    expect(command).toBe(
      "forgeops-agent.exe connect --code BYDPQC --backend ws://localhost:18000/api/v1/ws/agent",
    );
  });
});

describe("detectPlatform", () => {
  it("recognises Windows, macOS and Linux", () => {
    expect(detectPlatform({ platform: "Win32", userAgent: "" })).toBe("windows");
    expect(detectPlatform({ platform: "MacIntel", userAgent: "" })).toBe("macos");
    expect(detectPlatform({ platform: "Linux x86_64", userAgent: "" })).toBe("linux");
  });

  it("falls back to linux rather than guessing towards Windows", () => {
    // A wrong guess towards Windows prints `.exe` and a `.\` prefix to a macOS user, which is worse
    // than a wrong guess towards Linux, whose rendering is a plain POSIX command.
    expect(detectPlatform({ platform: "", userAgent: "" })).toBe("linux");
  });
});

describe("installOnPathCommand", () => {
  it("targets a directory already on PATH, needing no elevation on Windows", () => {
    const command = installOnPathCommand("windows", "powershell");
    expect(command).toContain("LOCALAPPDATA");
    // No elevation, and no PATH edit for the user to make.
    expect(command).not.toMatch(/RunAs|setx PATH/i);
  });

  it("uses install(1) on POSIX so the mode is set in the same step", () => {
    expect(installOnPathCommand("linux", "bash")).toBe(
      "sudo install -m 0755 ./forgeops-agent /usr/local/bin/forgeops-agent",
    );
  });
});

describe("archiveName", () => {
  it("matches GoReleaser's archive naming, lowercase and underscore separated", () => {
    expect(archiveName("windows", "v0.1.0", "amd64")).toBe(
      "forgeops-agent_0.1.0_windows_amd64.zip",
    );
    expect(archiveName("macos", "v0.1.0", "arm64")).toBe(
      "forgeops-agent_0.1.0_darwin_arm64.tar.gz",
    );
    expect(archiveName("linux", "0.1.0", "amd64")).toBe("forgeops-agent_0.1.0_linux_amd64.tar.gz");
  });
});

describe("AGENT_COMMANDS", () => {
  it("declares connect, which is the one command a first run needs", () => {
    expect(AGENT_COMMANDS.connect.flags).toContain("code");
    expect(AGENT_COMMANDS.connect.flags).toContain("backend");
  });

  it("does not claim scan takes a path", () => {
    expect(AGENT_COMMANDS.scan.flags).toEqual(["project"]);
  });
});

describe("CodeCountdown", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("shows the time remaining rather than a raw timestamp", () => {
    const start = Date.parse("2026-01-01T00:00:00Z");
    render(
      <CodeCountdown
        expiresAt="2026-01-01T00:05:00Z"
        onRemint={() => {}}
        isReminting={false}
        now={() => start}
      />,
    );
    // The defect was `expires_at` printed as an ISO string, leaving the user to subtract two times.
    expect(screen.getByTestId("code-countdown-value").textContent).toBe("5:00");
    expect(screen.queryByText("2026-01-01T00:05:00Z")).toBeNull();
  });

  it("counts down as time passes", () => {
    let clock = Date.parse("2026-01-01T00:00:00Z");
    render(
      <CodeCountdown
        expiresAt="2026-01-01T00:05:00Z"
        onRemint={() => {}}
        isReminting={false}
        now={() => clock}
      />,
    );
    expect(screen.getByTestId("code-countdown-value").textContent).toBe("5:00");
    clock += 61_000;
    // ct because the interval's setState happens outside React's batching otherwise, so the
    // component never re-renders and the assertion reads the first paint.
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(screen.getByTestId("code-countdown-value").textContent).toBe("3:59");
  });

  it("offers a re-mint in place once the code has expired", async () => {
    const onRemint = vi.fn();
    let clock = Date.parse("2026-01-01T00:00:00Z");
    render(
      <CodeCountdown
        expiresAt="2026-01-01T00:00:02Z"
        onRemint={onRemint}
        isReminting={false}
        now={() => clock}
      />,
    );
    clock += 3000;
    act(() => {
      vi.advanceTimersByTime(1000);
    });

    // THE DEFECT: the first code always expired on a first run, and the screen offered no way to
    // issue another without starting the flow again.
    expect(screen.getByTestId("code-expired")).toBeInTheDocument();
    await userEvent.click(screen.getByTestId("remint-code"));
    expect(onRemint).toHaveBeenCalledOnce();
  });

  it("says plainly that a code cannot be extended", () => {
    const clock = Date.parse("2026-01-01T00:10:00Z");
    render(
      <CodeCountdown
        expiresAt="2026-01-01T00:00:00Z"
        onRemint={() => {}}
        isReminting={false}
        now={() => clock}
      />,
    );
    expect(screen.getByTestId("code-expired").textContent).toMatch(/cannot be extended/);
  });

  it("does not announce every second to a screen reader", () => {
    const start = Date.parse("2026-01-01T00:00:00Z");
    render(
      <CodeCountdown
        expiresAt="2026-01-01T00:05:00Z"
        onRemint={() => {}}
        isReminting={false}
        now={() => start}
      />,
    );
    // `aria-live="polite"` on a per-second counter makes a screen reader unusable. It is switched on
    // only inside the last minute, which is the one moment the user needs telling.
    expect(screen.getByTestId("code-countdown").getAttribute("aria-live")).toBe("off");
  });

  it("announces once inside the final minute", () => {
    const start = Date.parse("2026-01-01T00:00:00Z");
    render(
      <CodeCountdown
        expiresAt="2026-01-01T00:00:30Z"
        onRemint={() => {}}
        isReminting={false}
        now={() => start}
      />,
    );
    expect(screen.getByTestId("code-countdown").getAttribute("aria-live")).toBe("polite");
  });
});

function renderPanel(props: Parameters<typeof AgentConnectPanel>[0]) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <AgentConnectPanel {...props} />
    </QueryClientProvider>,
  );
}

describe("AgentConnectPanel", () => {
  beforeEach(() => {
    mockGet.mockReset();
    mockGet.mockResolvedValue({
      backend_ws_url: "ws://localhost:18000/api/v1/ws/agent",
      agent_ws_path: "/api/v1/ws/agent",
      release_tag: "v0.1.0",
      download_base_url: "https://github.com/parag8487/ForgeOps/releases/download",
    });
  });

  it("renders a connect command carrying the backend the API reported", async () => {
    renderPanel({ code: "BYDPQC", platform: "windows" });

    // Waiting on the download offer, not on the command block: the command block is present from the
    // first paint with a placeholder backend, so `findByTestId` on it resolves before the query does
    // and the assertion would read the loading state.
    await screen.findByTestId("download-offer");

    const command = screen.getByTestId("connect-command");
    // The value is the deployment's, not a literal in the UI: BACKEND_PORT=18000 is what this stack
    // publishes, and a hardcoded port would be wrong for anybody who changed it.
    expect(command.textContent).toContain("--backend ws://localhost:18000/api/v1/ws/agent");
    expect(command.textContent).toContain("--code BYDPQC");
    expect(command.textContent).toContain("forgeops-agent.exe");
  });

  it("never tells the user to compile anything", async () => {
    renderPanel({ code: "BYDPQC", platform: "windows" });
    await screen.findByTestId("connect-command");

    const panel = screen.getByTestId("agent-connect-panel");
    // Building from source was the first step of the old flow and is not a user's problem. It may
    // still appear as a fallback when no download is published, which this deployment does publish.
    expect(panel.textContent).not.toMatch(/go build/);
  });

  it("offers a per-platform download for the release the deployment pins", async () => {
    renderPanel({ code: "BYDPQC", platform: "windows" });

    const offer = await screen.findByTestId("download-offer");
    const amd64 = within(offer).getByTestId("download-windows-amd64");
    expect(amd64.getAttribute("href")).toBe(
      "https://github.com/parag8487/ForgeOps/releases/download/v0.1.0/forgeops-agent_0.1.0_windows_amd64.zip",
    );
    // Both architectures, because a browser cannot reliably report arm64 and the wrong one produces
    // a binary that will not start.
    expect(within(offer).getByTestId("download-windows-arm64")).toBeInTheDocument();
  });

  it("says so plainly when the deployment publishes no download", async () => {
    mockGet.mockResolvedValue({
      backend_ws_url: "ws://localhost:18000/api/v1/ws/agent",
      agent_ws_path: "/api/v1/ws/agent",
      release_tag: "",
      download_base_url: "",
    });
    renderPanel({ code: "BYDPQC", platform: "linux" });

    // A link built from a tag the deployment does not pin would 404, and "the page is broken" is a
    // worse first experience than a plain statement.
    expect(await screen.findByTestId("download-unavailable")).toBeInTheDocument();
  });

  it("renders the install step for the chosen platform", async () => {
    renderPanel({ code: "BYDPQC", platform: "linux" });
    const install = await screen.findByTestId("install-command");
    expect(install.textContent).toContain("/usr/local/bin/forgeops-agent");
  });

  it("switches every command when the platform is changed", async () => {
    renderPanel({ code: "BYDPQC", platform: "windows" });
    await screen.findByTestId("connect-command");

    await userEvent.selectOptions(screen.getByTestId("platform-picker"), "macos");

    expect(screen.getByTestId("connect-command").textContent).not.toContain(".exe");
    expect(screen.getByTestId("install-command").textContent).toContain("install -m 0755");
  });

  it("offers a shell choice on Windows only, because that is where the invocation differs", async () => {
    renderPanel({ code: "BYDPQC", platform: "windows" });
    await screen.findByTestId("connect-command");
    expect(screen.getByTestId("shell-picker")).toBeInTheDocument();

    await userEvent.selectOptions(screen.getByTestId("platform-picker"), "linux");
    expect(screen.queryByTestId("shell-picker")).toBeNull();
  });

  it("offers doctor as the diagnostic, which now predicts a credential-store failure", async () => {
    renderPanel({ code: "BYDPQC", platform: "windows" });
    const doctor = await screen.findByTestId("doctor-command");
    expect(doctor.textContent).toBe("forgeops-agent.exe doctor");
  });
});
