import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../src/reeloom/server/static",
    emptyOutDir: true,
    assetsDir: "assets",
  },
  server: {
    host: "127.0.0.1",
    port: 4173,
    strictPort: true,
    proxy: { "/api": "http://127.0.0.1:8080" },
  },
  test: {
    environment: "jsdom",
    environmentOptions: { jsdom: { url: "http://localhost/" } },
    globals: true,
    setupFiles: "./tests/setup.ts",
    css: true,
    exclude: ["node_modules/**", "dist/**"],
  },
});
