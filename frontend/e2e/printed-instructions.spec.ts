/**
 * Part F: follow the printed instructions literally, as a user would.
 *
 * WHAT THIS EXISTS TO CATCH
 *
 * Every other spec runs the agent with arguments the SPEC chose:
 *
 *     composeExec("agent", ["forgeops-agent", "pair", "--code", code, "--backend", "ws://backend:8000/..."])
 *
 * That proves the CLI works. It proves nothing about the command the UI printed — and the UI printed
 * `forgeops-agent pair --code BYDPQC`, which fails three ways: PowerShell will not execute a bare name
 * from the current directory, the binary is `forgeops-agent.exe` on Windows, and `pair` refuses without
 * `--backend`. A separate rendered command once carried `scan --path`, a flag the CLI has never
 * accepted. Every one of those was a claim about the CLI made in a file that never met the CLI.
 *
 * So this spec reads the command strings OUT OF THE DOM and executes them verbatim. No arguments are
 * chosen here, nothing is appended, and nothing is corrected. If the printed command does not run,
 * this test fails.
 *
 * IT ALSO RUNS THE AGENT AS A HOST BINARY. The journey and onboarding specs run it inside a Linux
 * container, which is why the Windows keychain limit went unnoticed for so long. Here the binary is
 * built and executed on the runner itself, against the composed backend over its published port —
 * the same way a user runs it. `ci.yml`'s `agent-host` job covers the Windows and macOS credential
 * stores, which no Linux runner can.
 */

import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

import { expect, test, type Page } from "@playwright/test";

import { gotoAsOperator, mintAccessToken, OPERATOR, signIn } from "./helpers/auth";
import { eventually, sqlScalar } from "./helpers/stack";

test.describe.configure({ mode: "serial" });

const API = process.env.E2E_API_BASE_URL ?? "http://localhost:8000/api/v1";

/**
 * The repository root, resolved from the working directory rather than from `__dirname`.
 *
 * `frontend/package.json` declares `"type": "module"`, so `__dirname` does not exist — the first
 * version used it and the whole module failed to load, which Playwright reported as "No tests found".
 * A spec that cannot be loaded is worse than one that fails: the run went green-adjacent with zero
 * tests executed. `e2e/helpers/stack.ts` already resolves the root as
 * `path.resolve(process.cwd(), "..")`, and Playwright is invoked from `frontend/`, so this matches
 * what the rest of the suite does instead of inventing a second convention.
 */
const REPO_ROOT = resolve(process.cwd(), "..");

/** Unique per run, so a rerun does not collide with the previous run's project. */
const SUFFIX = Date.now().toString(36);
const PROJECT_NAME = `Printed ${SUFFIX}`;

const printed: {
  projectId?: string;
  code?: string;
  connectCommand?: string;
  installCommand?: string;
  workspace?: string;
  binDir?: string;
  deviceId?: string;
} = {};

function requireOperator() {
  test.skip(OPERATOR.password === "", "E2E_OIDC_PASSWORD is unset; provision-authentik.py sets it");
}

async function open(page: Page, path: string, heading: RegExp) {
  await gotoAsOperator(page, path);
  await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible({
    timeout: 30_000,
  });
}

/**
 * Run a command string the way a shell would, and return its output.
 *
 * `bash -lc` rather than splitting on spaces: the point is to execute what was PRINTED, and a printed
 * command may legitimately contain quoting that only a shell resolves. Splitting on whitespace here
 * would be this spec correcting the string, which is the one thing it must never do.
 */
function runPrinted(
  command: string,
  options: { cwd: string; env?: Record<string, string> },
): string {
  return execFileSync("bash", ["-lc", command], {
    cwd: options.cwd,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
    timeout: 300_000,
    env: { ...process.env, ...options.env },
  });
}

test.describe("Part F: the printed instructions, run verbatim", () => {
  test.afterAll(() => {
    // The agent is left running by the connect step, and a stray process holding a WebSocket would
    // outlive the run and confuse the next one.
    try {
      execFileSync("pkill", ["-f", "forgeops-agent connect"], { stdio: "ignore" });
    } catch {
      // Nothing to kill is the normal case when an earlier step failed.
    }
    if (printed.binDir) rmSync(printed.binDir, { recursive: true, force: true });
    if (printed.workspace) rmSync(printed.workspace, { recursive: true, force: true });
  });

  test("step 1 — a project exists, with a workspace on this machine", async ({ page }) => {
    requireOperator();
    await signIn(page);
    await mintAccessToken(page, API);

    // A real directory on the runner, with real content for the scan to find. Not the container's
    // /workspace: this spec runs the agent as a HOST process, so the path it indexes must be one the
    // host actually has.
    printed.workspace = mkdtempSync(join(tmpdir(), "forgeops-printed-"));
    mkdirSync(join(printed.workspace, "src"), { recursive: true });
    writeFileSync(
      join(printed.workspace, "package.json"),
      JSON.stringify(
        { name: "printed-fixture", version: "1.0.0", dependencies: { express: "4.19.2" } },
        null,
        2,
      ),
    );
    writeFileSync(
      join(printed.workspace, "src", "index.js"),
      "const express = require('express');\nconst app = express();\napp.listen(3000);\n",
    );

    await open(page, "/projects", /Projects/i);
    await page.getByLabel(/directory on the machine/i).check();
    await page.getByLabel("Name", { exact: true }).fill(PROJECT_NAME);
    await page.getByLabel(/working-tree path/i).fill(printed.workspace);
    await page.getByTestId("create-project").click();

    const id = await eventually("the project row", () =>
      sqlScalar(`SELECT id FROM projects WHERE name = '${PROJECT_NAME}'`),
    );
    expect(id).not.toBeNull();
    printed.projectId = id!;
  });

  test("step 2 — the onboarding screen prints a runnable install command", async ({ page }) => {
    requireOperator();
    await open(page, "/onboarding", /Getting started/i);
    await page.getByLabel("Project").selectOption(printed.projectId!);

    const panel = page.getByTestId("agent-connect-panel");
    await expect(panel).toBeVisible();

    // The panel must offer the reader's platform. The runner is Linux, so that is what it detected —
    // and the install command must be the POSIX one, not a PowerShell line.
    const install = await panel.getByTestId("install-command").innerText();
    printed.installCommand = install.trim();
    expect(printed.installCommand).toContain("forgeops-agent");
    expect(
      printed.installCommand,
      "a Linux reader must not be shown a PowerShell command",
    ).not.toContain("$env:");

    // NOTHING TO COMPILE. Building from source was the first step of the old flow, and it is what put
    // the five-minute code expiry out of reach.
    const wholePanel = await panel.innerText();
    expect(wholePanel, "the screen must not tell a user to build the agent").not.toMatch(
      /go build/,
    );
  });

  test("step 3 — the install command puts the binary on PATH, run exactly as printed", async () => {
    requireOperator();

    // The published archive is not available in CI, so the binary is built here — but the INSTALL step
    // that follows is the printed one, unmodified. What is under test is whether the printed command
    // installs a binary onto PATH, not where the binary came from.
    printed.binDir = mkdtempSync(join(tmpdir(), "forgeops-bin-"));
    execFileSync("go", ["build", "-o", join(printed.binDir, "forgeops-agent"), "./cmd/agent"], {
      cwd: join(REPO_ROOT, "agent"),
      stdio: "inherit",
      env: { ...process.env, CGO_ENABLED: "0" },
    });

    // The printed command is `sudo install -m 0755 ./forgeops-agent /usr/local/bin/forgeops-agent`,
    // run from the directory holding the download — so that is exactly how it is run.
    const output = runPrinted(printed.installCommand!, { cwd: printed.binDir });
    expect(output).toBeDefined();

    // THE POINT OF THE INSTALL STEP: the bare command now works, from anywhere, with no prefix.
    const version = runPrinted("forgeops-agent version", { cwd: tmpdir() });
    expect(version).toContain("forgeops-agent");
    // And no spurious shutdown noise, which on Windows appeared above every real message.
    expect(version.toLowerCase()).not.toContain("handle is invalid");
  });

  test("step 4 — doctor runs bare and reports the credential store", async () => {
    requireOperator();

    // `doctor` is what the panel offers when something goes wrong, so it must work as printed too.
    // It exits non-zero when Docker or Kubernetes is absent on the runner, which is a true report
    // about the machine rather than a failure of this test — so the output is what is asserted.
    let output = "";
    try {
      output = runPrinted("forgeops-agent doctor", {
        cwd: tmpdir(),
        env: { AGENT_STATE_DIR: join(printed.binDir!, "state") },
      });
    } catch (error) {
      output = String((error as { stdout?: string }).stdout ?? "");
    }
    expect(output).toContain("Credential store:");
    expect(
      output,
      "doctor must predict whether a credential fits before a code is spent",
    ).toContain("a device credential fits");
  });

  test("step 5 — a code is minted and the connect command carries it and the backend", async ({
    page,
  }) => {
    requireOperator();
    await open(page, "/onboarding", /Getting started/i);
    await page.getByLabel("Project").selectOption(printed.projectId!);

    await open(page, "/pairing", /Agent pairing/i);
    await page.getByLabel("Project").selectOption(printed.projectId!);
    await page.getByTestId("mint-code").click();

    const code = (await page.getByTestId("pairing-code-value").innerText()).trim();
    expect(code).toMatch(/^[0-9A-Z]{6}$/);
    printed.code = code;

    // A LIVE COUNTDOWN, not a raw timestamp. The old screen printed `expires_at` verbatim and left the
    // user to subtract two times; on a first run the code always expired before it could be used.
    await expect(page.getByTestId("code-countdown")).toBeVisible();
    const countdown = await page.getByTestId("code-countdown-value").innerText();
    expect(countdown, "the countdown must show minutes and seconds").toMatch(/^\d+:\d{2}$/);
    await expect(page.getByTestId("pairing-code")).not.toContainText(/\d{4}-\d{2}-\d{2}T/);

    // The command the user is told to run, read from the DOM.
    const connect = (await page.getByTestId("connect-command").innerText()).trim();
    printed.connectCommand = connect;

    // It must carry the code, and the --backend the old command was missing entirely.
    expect(connect).toContain(code);
    expect(connect, "the printed command must carry --backend or pair refuses").toContain(
      "--backend",
    );
    expect(connect, "a Linux reader must not be given a .exe").not.toContain(".exe");
    expect(connect, "the binary is on PATH, so no ./ prefix").not.toContain("./forgeops-agent");
  });

  test("step 6 — the printed connect command pairs, scans and stays running", async () => {
    requireOperator();
    const command = printed.connectCommand!;

    // EXECUTED VERBATIM. Nothing is appended and nothing is corrected — if this string does not run,
    // the screen is wrong and this test is the thing that says so.
    //
    // `connect` stays resident by design, so it is started detached and its output tailed. A
    // foreground run would hang this spec forever, which is the correct behaviour for the command and
    // the wrong behaviour for a test.
    const logPath = join(printed.binDir!, "connect.log");
    execFileSync(
      "bash",
      ["-lc", `cd ${printed.workspace} && nohup ${command} > ${logPath} 2>&1 &`],
      {
        encoding: "utf8",
        env: { ...process.env, AGENT_WORKSPACE_ROOT: printed.workspace! },
      },
    );

    // Stage 1: paired. Observed in the database rather than in the log, because the backend's row is
    // the fact and the log is a report of it.
    const deviceId = await eventually(
      "an active device for the project",
      () =>
        sqlScalar(
          `SELECT id FROM agent_devices WHERE project_id = '${printed.projectId}' AND status = 'active'`,
        ),
      { timeoutMs: 120_000 },
    );
    expect(deviceId, "the printed command did not pair; see connect.log").not.toBeNull();
    printed.deviceId = deviceId!;

    // Stage 2: scanned. The fixture has a package.json with a real dependency, so the index must have
    // files and the framework detector must have something to read.
    const indexed = await eventually(
      "an indexed codebase",
      () =>
        sqlScalar(
          `SELECT indexed_files FROM analysis_reports WHERE project_id = '${printed.projectId}' ORDER BY created_at DESC LIMIT 1`,
        ),
      { timeoutMs: 180_000 },
    );
    expect(Number(indexed ?? 0), "the printed command did not index the workspace").toBeGreaterThan(
      0,
    );

    // Stage 3: running. The agent holds a session, which is what makes an approved change set
    // applicable — and the whole reason `connect` does not exit.
    const lastSeen = await eventually(
      "a heartbeat from the running agent",
      () => sqlScalar(`SELECT last_seen FROM agent_devices WHERE id = '${printed.deviceId}'`),
      { timeoutMs: 120_000 },
    );
    expect(lastSeen, "the agent paired and scanned but is not holding a session").not.toBeNull();
  });

  test("step 7 — every stage was reported by name", async () => {
    requireOperator();

    // A single command replacing three must say what it did, or a failure is unattributable. The log is
    // read here rather than asserted on earlier, so the database assertions above stand on their own.
    const log = runPrinted(`cat ${join(printed.binDir!, "connect.log")}`, { cwd: tmpdir() });
    expect(log).toContain("[1/3] pair");
    expect(log).toContain("[2/3] scan");
    expect(log).toContain("[3/3] run");
    // And the backend it used, with where that value came from.
    expect(log).toContain("Backend:");
    expect(log).toMatch(/from the --backend flag/);
  });
});
