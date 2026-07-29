import { describe, expect, it } from "vitest";
import { readFileSync, existsSync } from "fs";
import { resolve } from "path";

const ROOT = resolve(__dirname, "..");
const pkg = JSON.parse(readFileSync(resolve(ROOT, "package.json"), "utf-8"));

describe("package-policy", () => {
  it("has private: true", () => {
    expect(pkg.private).toBe(true);
  });

  it("has license FSL-1.1-ALv2", () => {
    expect(pkg.license).toBe("FSL-1.1-ALv2");
  });

  it("all dependencies use exact version pins (no ^ or ~)", () => {
    const allDeps = { ...pkg.dependencies, ...pkg.devDependencies };
    const violations: string[] = [];
    for (const [name, version] of Object.entries(allDeps)) {
      const v = version as string;
      if (v.startsWith("^") || v.startsWith("~") || v.startsWith(">") || v.startsWith("<")) {
        violations.push(`${name}@${v}`);
      }
    }
    expect(violations).toEqual([]);
  });

  it("pnpm-lock.yaml is committed", () => {
    expect(existsSync(resolve(ROOT, "pnpm-lock.yaml"))).toBe(true);
  });

  it("does not contain forbidden future UI dependencies", () => {
    const forbidden = ["echarts", "xterm.js", "react-flow", "codemirror", "d2"];
    const allDeps = { ...pkg.dependencies, ...pkg.devDependencies };
    const depNames = Object.keys(allDeps);
    for (const lib of forbidden) {
      const found = depNames.find((name) => name === lib || name.includes(lib.replace(".js", "")));
      expect(found, `Forbidden dependency found: ${found}`).toBeUndefined();
    }
  });

  it("middleware.ts does not exist", () => {
    expect(existsSync(resolve(ROOT, "middleware.ts"))).toBe(false);
    expect(existsSync(resolve(ROOT, "src", "middleware.ts"))).toBe(false);
  });

  it("proxy.ts does not exist", () => {
    expect(existsSync(resolve(ROOT, "proxy.ts"))).toBe(false);
    expect(existsSync(resolve(ROOT, "src", "proxy.ts"))).toBe(false);
  });
});
