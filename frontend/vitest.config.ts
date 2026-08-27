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
        // `stores/**` and `hooks/**` were missing while everything else that ships to a browser was
        // listed. Omitting a whole directory is a stronger version of the failure mode described
        // above: excluding one badly-covered FILE at least appears in the table as a gap, whereas an
        // unlisted directory cannot move the number at all, no matter what it grows to contain.
        // `hooks/` is empty today and is listed so the first hook added lands inside the gate.
        "stores/**/*.{ts,tsx}",
        "hooks/**/*.{ts,tsx}",
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
      // WHY THESE ARE NOT 70. Criterion 11 asks for >=70% per component and the frontend measures
      // 95.81% statements / 85.34% branch / 95.47% functions / 95.92% lines. A floor of 70 against
      // that reality is not a gate, it is 25 points of permitted silent decay — a change could
      // delete most of the suite and still be green. The floor sits just under the measured numbers
      // so a REGRESSION fails while ordinary refactoring does not.
      //
      // Branches sit lower than the rest because they legitimately are: the remaining uncovered ones
      // are defensive `??` fallbacks on framework-supplied values. Raising this to match statements
      // would mean writing tests for cases the framework does not produce.
      //
      // Raise these when the numbers rise. Never lower them to make a change pass.
      thresholds: {
        statements: 90,
        lines: 90,
        functions: 90,
        branches: 80,
      },
    },
  },
});
