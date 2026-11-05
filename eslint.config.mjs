// Minimal ESLint flat config: recommended rules only, tuned so the current code passes.
import js from "@eslint/js";
import globals from "globals";

export default [
  { ignores: [".claude/**", ".idea/**", "node_modules/**", "handoff/**"] },
  js.configs.recommended,
  {
    files: ["**/*.js"],
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "script",
      globals: { ...globals.browser, ...globals.commonjs, ArControlModes: "readonly" },
    },
    rules: {
      "no-empty": ["error", { allowEmptyCatch: true }],
    },
  },
  {
    files: ["**/*.cjs"],
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "commonjs",
      globals: { ...globals.node },
    },
  },
  {
    // Code passed to page.evaluate() runs inside the app page and uses its globals.
    files: ["test_ar_ui.cjs"],
    rules: { "no-undef": "off" },
  },
];
