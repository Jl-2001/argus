import js from "@eslint/js";
import globals from "globals";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import tseslint from "typescript-eslint";
import { globalIgnores } from "eslint/config";

export default tseslint.config([
  globalIgnores(["dist", "node_modules"]),
  {
    files: ["**/*.{ts,tsx}"],
    extends: [js.configs.recommended, tseslint.configs.recommended, reactHooks.configs.flat["recommended-latest"]],
    plugins: { "react-refresh": reactRefresh.default ?? reactRefresh },
    languageOptions: {
      ecmaVersion: 2023,
      globals: globals.browser,
    },
    rules: {
      "react-refresh/only-export-components": ["warn", { allowConstantExport: true }],
      // Argus's own read-only-frontend convention: never a raw fetch()
      // call outside src/api/client.ts -- see
      // src/tests/api/readOnlyGuard.test.ts for the enforced version of
      // this; this lint rule is a fast, edit-time nudge toward the same
      // rule, not the actual guard.
      "no-restricted-globals": [
        "error",
        { name: "fetch", message: "Use src/api/client.ts's apiGet() instead of calling fetch() directly." },
      ],
    },
  },
  {
    // The one file allowed to call fetch() directly -- everything else
    // routes through it.
    files: ["src/api/client.ts"],
    rules: { "no-restricted-globals": "off" },
  },
  {
    // shadcn/ui-style wrapper files deliberately co-export a styled
    // component alongside the Radix primitives it wraps (Root/Trigger/
    // Content, ...); CitationContext.tsx/RealtimeProvider.tsx co-export
    // their Provider alongside the hook they define; ApplicationTopology.tsx
    // co-exports the pure buildTopology() helper alongside the component
    // specifically so it's unit-testable (see its own .test.tsx) --
    // all standard, intentional patterns; only Fast Refresh's own
    // dev-time hot-reload ergonomics are affected, never correctness.
    files: [
      "src/components/ui/**/*.tsx",
      "src/components/evidence/CitationContext.tsx",
      "src/components/topology/ApplicationTopology.tsx",
      "src/realtime/RealtimeProvider.tsx",
    ],
    rules: { "react-refresh/only-export-components": "off" },
  },
]);
