import { describe, expect, it } from "vitest";
import { readFileSync, existsSync } from "fs";
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
