import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CommandBlock } from "@/features/agent/CommandBlock";
import { installOnPathCommand, renderCommand } from "@/features/agent/commands";

/**
 * A command a user is about to paste into a shell, and the button that puts it on the clipboard.
 *
 * The copy path is worth testing because a silent failure is indistinguishable from success: a user
 * who believes they copied a pairing code and did not will paste whatever was on the clipboard before
 * into a terminal, and get an error about something unrelated.
 */

describe("CommandBlock", () => {
  const writeText = vi.fn();

  beforeEach(() => {
    writeText.mockReset();
    writeText.mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
      writable: true,
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the command verbatim, so what is read is what runs", () => {
    const command = renderCommand({
      verb: "connect",
      platform: "windows",
      shell: "powershell",
      flags: { code: "BYDPQC", backend: "ws://localhost:18000/api/v1/ws/agent" },
    });
    render(<CommandBlock command={command} testId="connect-command" />);
    expect(screen.getByTestId("connect-command").textContent).toBe(command);
  });

  it("shows a caption when given one", () => {
    render(
      <CommandBlock command="forgeops-agent doctor" caption="Check prerequisites" testId="c" />,
    );
    expect(screen.getByText("Check prerequisites")).toBeInTheDocument();
  });

  it("copies the exact command, not the rendered text with wrapping", async () => {
    // The element wraps visually with `break-all` so a long --backend URL fits the panel. What goes
    // on the clipboard must be the original string: a copied command with a newline injected by
    // wrapping is two commands, and the second one is nonsense.
    const command =
      "forgeops-agent.exe connect --code BYDPQC --backend ws://localhost:18000/api/v1/ws/agent";
    render(<CommandBlock command={command} testId="connect-command" />);

    await userEvent.click(screen.getByTestId("copy-connect-command"));
    expect(writeText).toHaveBeenCalledWith(command);
  });

  it("confirms the copy, because a silent copy cannot be told from a dead button", async () => {
    render(<CommandBlock command="forgeops-agent doctor" testId="doctor-command" />);
    const button = screen.getByTestId("copy-doctor-command");
    expect(button.textContent).toBe("Copy");

    await userEvent.click(button);
    expect(button.textContent).toBe("Copied");
  });

  it("reports a failed copy rather than claiming success", async () => {
    // `navigator.clipboard` is unavailable on an insecure origin and can be denied by permission.
    // Both must be visible: a user who thinks they have the code and does not is worse off than one
    // who knows they need to select it by hand.
    writeText.mockRejectedValue(new Error("permission denied"));
    render(<CommandBlock command="forgeops-agent doctor" testId="doctor-command" />);

    await userEvent.click(screen.getByTestId("copy-doctor-command"));
    expect(screen.getByTestId("copy-doctor-command").textContent).toBe("Copy failed");
  });

  it("has an accessible label naming what it copies", () => {
    render(<CommandBlock command="forgeops-agent doctor" testId="doctor-command" />);
    // "Copy" alone tells a screen-reader user nothing when three of them are on one screen.
    expect(screen.getByTestId("copy-doctor-command").getAttribute("aria-label")).toContain(
      "doctor-command",
    );
  });
});

describe("installOnPathCommand for cmd.exe", () => {
  it("delegates to PowerShell rather than pretending cmd can do it readably", () => {
    // cmd.exe has no readable one-liner for "make a directory and move a file into it", so the honest
    // answer is to hand it the PowerShell form to run. Silently printing the PowerShell version
    // WITHOUT the `powershell -Command` wrapper would produce a line that fails in the shell the user
    // told us they were in.
    const command = installOnPathCommand("windows", "cmd");
    expect(command.startsWith("powershell -NoProfile -Command")).toBe(true);
    expect(command).toContain("LOCALAPPDATA");
  });

  it("gives PowerShell the native form, with no wrapper", () => {
    const command = installOnPathCommand("windows", "powershell");
    expect(command.startsWith("powershell")).toBe(false);
    expect(command).toContain("Move-Item");
  });

  it("uses the same target directory in both Windows forms", () => {
    // Two commands that install to different places would leave a user with the binary on PATH in
    // one shell and not the other.
    const viaCmd = installOnPathCommand("windows", "cmd");
    const viaPowerShell = installOnPathCommand("windows", "powershell");
    expect(viaCmd).toContain("Programs\\ForgeOps");
    expect(viaPowerShell).toContain("Programs\\ForgeOps");
  });

  it("gives macOS the same POSIX install as Linux", () => {
    // THE SAME MECHANISM, NOT THE SAME STRING. Both extract a tarball and then `install(1)` it,
    // but each names its own archive - the release asset is `..._darwin_...` on macOS and
    // `..._linux_...` on Linux, and a command that globbed for the wrong one would match nothing.
    const macos = installOnPathCommand("macos", "bash");
    const linux = installOnPathCommand("linux", "bash");
    expect(macos).toContain("forgeops-agent_*_darwin_*.tar.gz");
    expect(linux).toContain("forgeops-agent_*_linux_*.tar.gz");
    const install = 'sudo install -m 0755 "$t/forgeops-agent" /usr/local/bin/forgeops-agent';
    expect(macos).toContain(install);
    expect(linux).toContain(install);
    // `replaceAll`, not `replace`: the goos token appears twice now - once for the current folder
    // and once for `~/Downloads` - and a single replacement would leave the second one as `darwin`
    // and fail for a reason that has nothing to do with the property under test.
    expect(macos.replaceAll("darwin", "linux")).toBe(linux);
    // BOTH LOCATIONS. The archive is wherever the browser put it, and a command that searched only
    // the current directory told a user standing in their home folder that nothing was found while
    // the file sat in `Downloads` one level down.
    expect(macos).toContain("~/Downloads/");
    expect(linux).toContain("~/Downloads/");
  });
});
