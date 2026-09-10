import { describe, expect, it } from "vitest";
import { readFileSync, existsSync, readdirSync, statSync } from "fs";
import { resolve } from "path";

const ROOT = resolve(__dirname, "..");

describe("Playwright config static validation", () => {
  it("playwright.config.ts exists", () => {
    expect(existsSync(resolve(ROOT, "playwright.config.ts"))).toBe(true);
  });

  it("playwright.config.ts does not contain watch mode", () => {
    const content = readFileSync(resolve(ROOT, "playwright.config.ts"), "utf-8");
    // Should not have watch mode enabled
    expect(content).not.toMatch(/watch:\s*true/);
    // Should not contain --watch flag
    expect(content).not.toContain("--watch");
  });

  it("playwright.config.ts uses single-run mode (no repeat)", () => {
    const content = readFileSync(resolve(ROOT, "playwright.config.ts"), "utf-8");
    expect(content).not.toMatch(/repeatEach:\s*[2-9]/);
  });

  it("e2e/shell.spec.ts exists", () => {
    expect(existsSync(resolve(ROOT, "e2e", "shell.spec.ts"))).toBe(true);
  });
});

describe("k6 health smoke script static validation", () => {
  it("load/health.js exists", () => {
    expect(existsSync(resolve(ROOT, "load", "health.js"))).toBe(true);
  });

  it("k6 script targets /health endpoint", () => {
    const content = readFileSync(resolve(ROOT, "load", "health.js"), "utf-8");
    expect(content).toContain("/health");
  });

  it("k6 script has valid options with VUs and duration", () => {
    const content = readFileSync(resolve(ROOT, "load", "health.js"), "utf-8");
    expect(content).toContain("vus");
    expect(content).toContain("duration");
  });

  it("k6 script checks status 200", () => {
    const content = readFileSync(resolve(ROOT, "load", "health.js"), "utf-8");
    expect(content).toContain("200");
  });
});

describe("vitest config static validation", () => {
  it("vitest config uses --run mode (watch: false)", () => {
    const content = readFileSync(resolve(ROOT, "vitest.config.ts"), "utf-8");
    expect(content).toContain("watch: false");
  });
});

describe("Makefile load target validation", () => {
  it("root Makefile contains a load target", () => {
    const makefile = readFileSync(resolve(ROOT, "..", "Makefile"), "utf-8");
    expect(makefile).toContain("load:");
  });

  it("load target references k6", () => {
    const makefile = readFileSync(resolve(ROOT, "..", "Makefile"), "utf-8");
    expect(makefile).toContain("k6");
  });
});

describe("no caller may spell the API base path", () => {
  /**
   * `api.get("/api/v1/...")` is always a bug, and it shipped once.
   *
   * `lib/api/client.ts` builds every request as `${NEXT_PUBLIC_API_BASE_URL}${path}`, and that base
   * already ends in `/api/v1`. `CompiledPromptPanel` passed `/api/v1/generation/runs/${runId}`, so the
   * browser requested `/api/v1/api/v1/generation/runs/...` and got a 404 that the panel reported as
   * "Not Found" - a message a reader cannot tell apart from a run that genuinely does not exist.
   *
   * ITS OWN TEST DID NOT CATCH IT because the test mocked `api.get` and asserted the same wrong string.
   * A mock has no base URL to double up, so both sides agreed and both were wrong. This checks the
   * SOURCE instead, which is the only place the mistake is visible.
   */
  const DIRS = ["app", "components", "features", "lib", "hooks", "stores"];

  function sourceFiles(dir: string): string[] {
    const full = resolve(ROOT, dir);
    if (!existsSync(full)) return [];
    const out: string[] = [];
    for (const entry of readdirSync(full)) {
      const child = resolve(full, entry);
      if (statSync(child).isDirectory()) {
        out.push(...sourceFiles(resolve(dir, entry)));
      } else if (/\.(ts|tsx)$/.test(entry)) {
        out.push(child);
      }
    }
    return out;
  }

  it("passes a bare path to every api call, never one starting with the base", () => {
    const offenders: string[] = [];
    const call =
      /\bapi\s*\.\s*(?:get|post|put|patch|delete|deleteWith|stream)\s*(?:<[^>]*>)?\s*\(\s*[`"'](\/api\/v\d)/;

    for (const file of DIRS.flatMap(sourceFiles)) {
      readFileSync(file, "utf8")
        .split("\n")
        .forEach((line, index) => {
          if (call.test(line)) {
            offenders.push(`${file.replace(ROOT, "")}:${index + 1}: ${line.trim()}`);
          }
        });
    }

    expect(
      offenders,
      `these calls prepend the API base a second time, producing /api/v1/api/v1/...:\n${offenders.join("\n")}`,
    ).toEqual([]);
  });

  it("finds the files it is meant to be scanning", () => {
    // Without this the walk could silently match nothing - a guard that examines no files passes.
    expect(DIRS.flatMap(sourceFiles).length).toBeGreaterThan(40);
  });
});
