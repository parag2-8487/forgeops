/**
 * Every agent command string the UI renders, in one place, per shell.
 *
 * WHY THIS MODULE EXISTS
 *
 * The Pairing screen told users to run:
 *
 *     forgeops-agent pair --code BYDPQC
 *
 * which does not work on Windows for two independent reasons. PowerShell does not search the
 * current directory, so an unqualified name is not found even when the file is right there; and the
 * binary is `forgeops-agent.exe`. A user has to know to type `.\forgeops-agent.exe` instead, and
 * nothing told them. The command was also missing `--backend`, without which `pair` refuses.
 *
 * Separately, a rendered command once carried `scan --path`, a flag the CLI has never accepted. It
 * was found by running it.
 *
 * So: one module, one shape for a command, and `scripts/check-rendered-commands.py` cross-checks
 * every flag named here against the flags the Go source actually registers. An invented flag fails
 * a gate instead of a user.
 */

/** The shells a command can be rendered for. */
export type Shell = "powershell" | "cmd" | "bash";

/** The platforms the UI can detect and offer a download for. */
export type Platform = "windows" | "macos" | "linux";

/**
 * How each shell invokes a program that sits in the current directory.
 *
 * This table is the defect, expressed as data. PowerShell requires the `.\` prefix because it
 * deliberately does not search the current directory -- running a program from the working directory
 * by bare name is how a malicious `ls.exe` gets executed, so PowerShell refuses. cmd.exe does search
 * it, so the bare name works there. POSIX shells need `./` for the same reason PowerShell needs
 * `.\`.
 */
const LOCAL_INVOCATION: Record<Shell, (binary: string) => string> = {
  powershell: (binary) => `.\\${binary}`,
  cmd: (binary) => binary,
  bash: (binary) => `./${binary}`,
};

/** The binary's name on each platform. */
const BINARY_NAME: Record<Platform, string> = {
  windows: "forgeops-agent.exe",
  macos: "forgeops-agent",
  linux: "forgeops-agent",
};

/** Which shell a platform gets by default. */
const DEFAULT_SHELL: Record<Platform, Shell> = {
  windows: "powershell",
  macos: "bash",
  linux: "bash",
};

/**
 * The agent's real command surface.
 *
 * DECLARED AS DATA SO A GATE CAN CHECK IT. `scripts/check-rendered-commands.py` parses
 * `agent/internal/app/commands.go` for the verbs it registers and the flags each one declares, and
 * fails if anything here is absent there. That is the check that would have caught `scan --path`.
 */
export const AGENT_COMMANDS = {
  pair: { verb: "pair", flags: ["code", "backend", "wipe"] },
  connect: { verb: "connect", flags: ["code", "backend", "project", "workspace"] },
  run: { verb: "run", flags: [] },
  scan: { verb: "scan", flags: ["project"] },
  watch: { verb: "watch", flags: ["project", "debounce", "once"] },
  doctor: { verb: "doctor", flags: [] },
  version: { verb: "version", flags: [] },
} as const;

export type Verb = keyof typeof AGENT_COMMANDS;

/** Where the binary is, which decides how it is invoked. */
export type Location =
  | { kind: "current-directory" }
  /** Installed on PATH, so the bare name works in every shell. */
  | { kind: "on-path" };

export interface CommandRequest {
  verb: Verb;
  /** Flag values. `true` renders a bare boolean flag; `false` and `undefined` omit it. */
  flags?: Partial<Record<string, string | number | boolean | undefined>>;
  platform: Platform;
  shell?: Shell;
  location?: Location;
}

/**
 * Quote a value for the given shell, if it needs quoting.
 *
 * A project path is the realistic case: `C:\Users\Someone\My Project` has a space in it, and an
 * unquoted rendering silently passes two arguments. PowerShell and cmd both accept double quotes;
 * POSIX shells accept single quotes and treat everything inside literally, which is what a Windows
 * path pasted into WSL needs.
 */
function quote(value: string, shell: Shell): string {
  if (value === "") return shell === "bash" ? "''" : '""';
  if (!/[\s"'`$&|<>^]/.test(value)) return value;
  if (shell === "bash") return `'${value.replace(/'/g, `'\\''`)}'`;
  return `"${value.replace(/"/g, '""')}"`;
}

/**
 * Render one command, correct for the platform and shell, ready to paste unmodified.
 *
 * Throws on an unknown verb or an unknown flag rather than rendering it. A component that asks for
 * a flag the CLI does not have is a bug in the caller, and a thrown error surfaces it in the test
 * that renders the component -- which is precisely how `scan --path` should have been caught.
 */
export function renderCommand(request: CommandRequest): string {
  const spec = AGENT_COMMANDS[request.verb];
  if (spec === undefined) {
    throw new Error(`renderCommand: unknown verb ${String(request.verb)}`);
  }

  const shell = request.shell ?? DEFAULT_SHELL[request.platform];
  const binary = BINARY_NAME[request.platform];
  const location = request.location ?? { kind: "on-path" };
  const program = location.kind === "on-path" ? binary : LOCAL_INVOCATION[shell](binary);

  const parts = [program, spec.verb];
  for (const [name, value] of Object.entries(request.flags ?? {})) {
    if (!(spec.flags as readonly string[]).includes(name)) {
      throw new Error(
        `renderCommand: \`${spec.verb}\` has no --${name} flag. Its flags are: ` +
          `${spec.flags.join(", ") || "(none)"}. Adding one here without adding it to the CLI is ` +
          `how \`scan --path\` reached a user.`,
      );
    }
    if (value === undefined || value === false) continue;
    if (value === true) {
      parts.push(`--${name}`);
      continue;
    }
    parts.push(`--${name}`, quote(String(value), shell));
  }
  return parts.join(" ");
}

/**
 * Detect the platform from the browser, defaulting to Windows only when it is actually indicated.
 *
 * `navigator.userAgentData.platform` is preferred where available; `navigator.platform` is
 * deprecated but is the only thing Firefox and Safari offer. When neither says anything useful the
 * answer is `linux`, because that is the one platform where the rendered command is identical
 * whether the guess was right or not -- a wrong guess towards Windows would print `.exe` to a
 * macOS user.
 */
export function detectPlatform(navigatorLike?: {
  userAgent?: string;
  platform?: string;
}): Platform {
  const source =
    navigatorLike ??
    (typeof navigator === "undefined"
      ? undefined
      : { userAgent: navigator.userAgent, platform: navigator.platform });
  const haystack = `${source?.platform ?? ""} ${source?.userAgent ?? ""}`.toLowerCase();

  if (haystack.includes("win")) return "windows";
  if (haystack.includes("mac") || haystack.includes("darwin")) return "macos";
  return "linux";
}

/** The shells worth offering for a platform, most likely first. */
export function shellsFor(platform: Platform): readonly Shell[] {
  return platform === "windows" ? (["powershell", "cmd"] as const) : (["bash"] as const);
}

/** Human labels for the shell picker. */
export const SHELL_LABELS: Record<Shell, string> = {
  powershell: "PowerShell",
  cmd: "Command Prompt",
  bash: "bash / zsh",
};

/** Human labels for the platform picker. */
export const PLATFORM_LABELS: Record<Platform, string> = {
  windows: "Windows",
  macos: "macOS",
  linux: "Linux",
};

/**
 * The archive name GoReleaser produces for a platform.
 *
 * Matches `agent/.goreleaser.yaml`'s `name_template`. Asserted against it by
 * `scripts/check-rendered-commands.py`, so a rename there fails a gate rather than producing a
 * download link that 404s.
 */
export function archiveName(platform: Platform, tag: string, arch: "amd64" | "arm64"): string {
  const version = tag.replace(/^v/, "");
  const goos = platform === "macos" ? "darwin" : platform;
  const extension = platform === "windows" ? "zip" : "tar.gz";
  return `forgeops-agent_${version}_${goos}_${arch}.${extension}`;
}

/**
 * The one documented step that puts the binary on PATH, per platform.
 *
 * `make build-agent` stays for developers and is deliberately not this. A user should never be told
 * to compile anything, and building from source was the first step of the old flow.
 */
/**
 * The Windows install step, as one line.
 *
 * Declared once because it is rendered for two shells: cmd.exe receives it wrapped in
 * `powershell -NoProfile -Command`, and a second copy of the text would be a second thing to keep
 * correct. Single quotes inside so the cmd.exe wrapper's double quotes do not need escaping.
 */
/**
 * The Windows install step, as one line.
 *
 * NOT A SINGLE DOUBLE QUOTE IN IT, and that is the reason it is written with `Join-Path` and string
 * concatenation rather than the shorter interpolated form. This text is rendered twice: verbatim for
 * PowerShell, and wrapped in `powershell -NoProfile -Command "..."` for cmd.exe. Any double quote
 * inside would need escaping for the second case, the escaping would have to survive being a
 * TypeScript literal as well, and the first attempt at this shipped a command containing a literal
 * `\"` that PowerShell could not parse. Removing the character removes the whole class.
 *
 * `Join-Path` also handles a username with a space in it, which a bare unquoted path would not.
 */
const WINDOWS_INSTALL =
  "$d = Join-Path $env:LOCALAPPDATA 'Programs\\ForgeOps'; " +
  "New-Item -ItemType Directory -Force $d | Out-Null; " +
  "Move-Item -Force .\\forgeops-agent.exe $d; " +
  "$u = [Environment]::GetEnvironmentVariable('PATH','User'); " +
  "if ($u -notlike ('*' + $d + '*')) " +
  "{ [Environment]::SetEnvironmentVariable('PATH', ($u + ';' + $d), 'User') }; " +
  "$env:PATH = ($env:PATH + ';' + $d)";

export function installOnPathCommand(platform: Platform, shell: Shell): string {
  switch (platform) {
    case "windows":
      // IT ADDS THE DIRECTORY TO PATH, and the previous version did not.
      //
      // This command used to copy the binary into `$env:LOCALAPPDATA\Programs\ForgeOps` and stop,
      // under a comment asserting that `$env:LOCALAPPDATA\Programs` "is on PATH for the current user
      // on a default Windows install". That is false, and a user following the screen hit it
      // immediately: the move succeeded and the very next printed command answered
      //
      //     forgeops-agent.exe : The term 'forgeops-agent.exe' is not recognized as the name of a
      //     cmdlet, function, script file, or operable program.
      //
      // Measured on a real Windows install: that directory is on NEITHER the process PATH nor the
      // persisted user PATH. Every other tool living there — Ollama, VS Code, Python — is on PATH
      // because its own installer put it there. The parent is not special.
      //
      // BOTH SCOPES ARE SET, deliberately. `SetEnvironmentVariable(..., "User")` persists it so future
      // shells work, and it alone would leave the CURRENT shell still failing — which is the same
      // "I did what it said and it did not work" experience one step later. Assigning `$env:PATH` as
      // well means the next printed command runs in the terminal already open.
      //
      // The `-notlike` guard makes it idempotent: running the install twice must not append a second
      // copy, because a PATH that grows on every run is a slow leak nobody attributes to this.
      //
      // No elevation: `HKCU` and a per-user directory only. cmd.exe cannot express this readably in
      // one line, so it is handed the PowerShell form to run, which is honest about what it is.
      return shell === "cmd"
        ? 'powershell -NoProfile -Command "' + WINDOWS_INSTALL + '"'
        : WINDOWS_INSTALL;
    case "macos":
    case "linux":
      // `/usr/local/bin` is on PATH everywhere and `install` sets the mode in the same step, so
      // there is no separate chmod to forget.
      return "sudo install -m 0755 ./forgeops-agent /usr/local/bin/forgeops-agent";
  }
}
