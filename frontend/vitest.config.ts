import react from "@vitejs/plugin-react";
import { resolve } from "path";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": resolve(__dirname, "."),
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./vitest.setup.ts"],
    include: ["**/*.test.{ts,tsx}"],
    watch: false,
    // Coverage is a GATE, not a report (criterion 11, D-31). There was no `coverage` key here
    // at all while PROGRESS.md recorded "frontend holds vitest v8 thresholds at 70/70/70" as
    // evidence that the gate was on and green. See LEARNING-JOURNAL finding 81.
    coverage: {
      provider: "v8",
      reporter: ["text", "text-summary"],
      // Scoped to application source. Everything listed is code that ships to a browser;
      // nothing is excluded because it happens to be poorly covered, which is the failure mode
      // that makes a coverage gate meaningless. `app/**` is included even though route
      // components are thin, precisely so a route added without a test moves the number down.
      include: [
        "app/**/*.{ts,tsx}",
        "components/**/*.{ts,tsx}",
        "features/**/*.{ts,tsx}",
        "lib/**/*.{ts,tsx}",
      ],
      exclude: [
        "**/*.test.{ts,tsx}",
        "**/*.d.ts",
        // Next.js framework entry points with no logic to assert: the root layout is provider
        // composition and these two are framework-invoked error surfaces.
        "app/layout.tsx",
        "app/error.tsx",
        "app/not-found.tsx",
      ],
      thresholds: {
        lines: 70,
        functions: 70,
        branches: 70,
      },
    },
  },
});
