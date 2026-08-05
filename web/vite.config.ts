import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../src/reeloom/server/static",
    // Hashed assets may still be served by an already-open browser during an
    // upgrade. Preserve prior bundles; the repository also forbids deletion.
    emptyOutDir: false,
    manifest: "manifest.json",
    assetsDir: "assets",
  },
  server: {
    host: "127.0.0.1",
    port: 4173,
    strictPort: true,
  },
  test: {
    environment: "jsdom",
    environmentOptions: {
      jsdom: { url: "http://localhost/" },
    },
    globals: true,
    setupFiles: "./tests/setup.ts",
    css: true,
    exclude: ["e2e/**", "node_modules/**", "dist/**"],
  },
});
