// SPDX-License-Identifier: FSL-1.1-ALv2
/**
 * Helpers that let the journey assert on things a browser cannot see.
 *
 * WHY THESE EXIST
 * §12.6's rule is that every step asserts an HTTP status, a row in the database, or a file on disk —
 * never rendered text alone. The version of `journey.spec.ts` this replaces asserted that the string
 * "Projects" was visible somewhere, which a hardcoded dashboard satisfied while nothing was
 * persisted. Text is the one thing a page can produce without the system having done anything.
 *
 * So: `sql()` reaches the real Postgres, `agentFile()` reads the real filesystem inside the agent
 * container, and `composeExec()` runs the real agent binary. All three shell out through
 * `docker compose`, which is deliberate — the alternative is a second connection path that could
 * succeed while the containers' own view differs.
 */
import { execFileSync } from "node:child_process";
import path from "node:path";

const REPO_ROOT = path.resolve(process.cwd(), "..");

/** Both files, in the order `docker compose` must see them. */
const COMPOSE_ARGS = [
  "compose",
  "-f",
  path.join(REPO_ROOT, "docker-compose.yml"),
  "-f",
  path.join(REPO_ROOT, "docker-compose.e2e.yml"),
];

export class StackCommandError extends Error {
  constructor(
    readonly argv: string[],
    readonly status: number | null,
    readonly stdout: string,
    readonly stderr: string,
  ) {
    super(
      `docker ${argv.join(" ")} exited ${status}\n` +
        `--- stdout ---\n${stdout}\n--- stderr ---\n${stderr}`,
    );
    this.name = "StackCommandError";
  }
}

function docker(args: string[], { allowFailure = false } = {}): string {
  try {
    return execFileSync("docker", args, {
      cwd: REPO_ROOT,
      encoding: "utf8",
      // Generous: the agent's first apply pulls nothing but does real work, and a tight timeout
      // turns a slow machine into a false failure.
      timeout: 180_000,
      maxBuffer: 32 * 1024 * 1024,
    });
  } catch (error) {
    const e = error as { status?: number; stdout?: string; stderr?: string };
    if (allowFailure) return e.stdout ?? "";
    throw new StackCommandError(args, e.status ?? null, e.stdout ?? "", e.stderr ?? "");
  }
}

/** Run a command inside a running service. */
export function composeExec(
  service: string,
  argv: string[],
  options?: { allowFailure?: boolean },
): string {
  // `-T` disables TTY allocation, without which this hangs in CI.
  return docker([...COMPOSE_ARGS, "exec", "-T", service, ...argv], options);
}

/** Start a long-running command inside a service and return immediately. */
export function composeExecDetached(service: string, argv: string[]): void {
  docker([...COMPOSE_ARGS, "exec", "-d", "-T", service, ...argv]);
}

export function composeLogs(service: string): string {
  return docker([...COMPOSE_ARGS, "logs", "--no-color", service], { allowFailure: true });
}

/**
 * One row per line, columns tab-separated, no headers or padding.
 *
 * `-A` and `-t` matter: without them psql pads columns to a fixed width and a hash comparison fails
 * on whitespace that was never in the database.
 */
export function sql(query: string): string[][] {
  const raw = composeExec("postgres", [
    "psql",
    "-U",
    process.env.POSTGRES_USER ?? "forgeops",
    "-d",
    process.env.POSTGRES_DB ?? "forgeops",
    "-A",
    "-t",
    "-F",
    "\t",
    "-c",
    query,
  ]);
  return raw
    .split("\n")
    .map((line) => line.replace(/\r$/, ""))
    .filter((line) => line.trim() !== "")
    .map((line) => line.split("\t"));
}

/** A single scalar, or null when the query returned no rows. */
export function sqlScalar(query: string): string | null {
  const rows = sql(query);
  return rows.length === 0 ? null : rows[0][0];
}

/** The exact bytes of a file inside the agent container, or null when it does not exist. */
export function agentFile(relativePath: string): string | null {
  const probe = composeExec(
    "agent",
    [
      "sh",
      "-c",
      `if [ -f '/workspace/${relativePath}' ]; then cat '/workspace/${relativePath}'; else echo '__ABSENT__'; fi`,
    ],
    { allowFailure: true },
  );
  if (probe.trim() === "__ABSENT__") return null;
  return probe;
}

/** SHA-256 of a file inside the agent container, computed BY the container. */
export function agentFileSha256(relativePath: string): string | null {
  const out = composeExec(
    "agent",
    ["sh", "-c", `sha256sum '/workspace/${relativePath}' 2>/dev/null | cut -d' ' -f1 || true`],
    { allowFailure: true },
  );
  const digest = out.trim();
  return /^[0-9a-f]{64}$/.test(digest) ? digest : null;
}

/** Paths under a directory in the agent container, relative to the workspace. */
export function agentList(relativeDir: string): string[] {
  const out = composeExec(
    "agent",
    ["sh", "-c", `cd /workspace && find '${relativeDir}' -type f 2>/dev/null | sort || true`],
    { allowFailure: true },
  );
  return out
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line !== "");
}

/** Poll until the predicate holds, so a step does not race the agent's asynchronous work. */
export async function eventually<T>(
  describe: string,
  probe: () => T | null | undefined,
  { timeoutMs = 60_000, intervalMs = 1_000 }: { timeoutMs?: number; intervalMs?: number } = {},
): Promise<T> {
  const deadline = Date.now() + timeoutMs;
  let last: unknown = undefined;
  while (Date.now() < deadline) {
    const value = probe();
    if (value !== null && value !== undefined && value !== false) return value as T;
    last = value;
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
  throw new Error(
    `timed out after ${timeoutMs}ms waiting for ${describe}; last observation was ${JSON.stringify(last)}`,
  );
}
