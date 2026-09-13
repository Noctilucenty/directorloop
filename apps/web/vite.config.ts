import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The studio uses the local API by default. Recorded preview is an explicit offline mode.
export default defineConfig(({ mode }) => ({
  plugins: [react()],
  define: {
    "import.meta.env.VITE_API_MODE": JSON.stringify(process.env.VITE_API_MODE ?? (mode === "recorded" ? "mock" : "live")),
  },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: process.env.DL_WEB_API_PROXY ?? "http://127.0.0.1:8787", changeOrigin: false },
      "/media": { target: process.env.DL_WEB_API_PROXY ?? "http://127.0.0.1:8787", changeOrigin: false },
    },
  },
  build: { outDir: "dist", sourcemap: false },
}));
