// ESLint flat config.
//
// eslint-config-next 16 already EXPORTS flat config arrays, so it is spread in
// directly. The previous version routed it through `FlatCompat`, the legacy
// .eslintrc bridge, which crashes under ESLint 10 with
// "TypeError: Converting circular structure to JSON" — @eslint/eslintrc tries to
// JSON.stringify the config while formatting a schema-validation error, and the
// Next plugin object references itself. Using the native export removes the
// bridge entirely rather than working around it.
import nextCoreWebVitals from "eslint-config-next/core-web-vitals";
import prettierConfig from "eslint-config-prettier";

const eslintConfig = [
  ...nextCoreWebVitals,
  {
    // eslint-plugin-react otherwise probes the filesystem to detect the React
    // version and throws inside resolveBasedir under pnpm's symlinked store.
    // Declaring it removes the probe; keep this in step with package.json.
    settings: { react: { version: "19.0" } },
  },
  // Must come last: turns off the stylistic rules that would fight Prettier,
  // which owns formatting in this repo (design §16.3).
  prettierConfig,
  {
    ignores: [
      ".next/",
      "node_modules/",
      "coverage/",
      "playwright-report/",
      "test-results/",
      "next-env.d.ts",
    ],
  },
];

export default eslintConfig;
