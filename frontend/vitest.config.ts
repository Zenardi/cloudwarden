import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Frontend test harness (introduced with #114). Coverage is scoped to the
// modules this milestone adds so the ≥95% gate measures *new* code rather than
// being diluted by the large, pre-existing page/api surface.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: ["./vitest.setup.ts"],
    include: ["app/**/*.test.{ts,tsx}"],
    coverage: {
      provider: "v8",
      reporter: ["text", "text-summary"],
      include: [
        "app/lib/trend.ts",
        "app/lib/savings.ts",
        "app/lib/scope.ts",
        "app/lib/roving.ts",
        "app/components/Sparkline.tsx",
        "app/components/CostTrend.tsx",
        "app/components/RangeControl.tsx",
        "app/components/RefreshStatus.tsx",
        "app/components/ScopeControls.tsx",
      ],
      thresholds: { lines: 95, functions: 95, statements: 95, branches: 90 },
    },
  },
});
