import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// API mode: `npm run dev` defaults to the in-browser mock, `npm run build` defaults to the live
// same-origin /api. Override either with VITE_API_MODE=mock|live in the environment.
export default defineConfig(({ command }) => ({
  plugins: [react()],
  define: {
    "import.meta.env.VITE_API_MODE": JSON.stringify(
      process.env.VITE_API_MODE ?? (command === "build" ? "live" : "mock"),
    ),
  },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8787", changeOrigin: false },
    },
  },
  build: { outDir: "dist", sourcemap: false },
}));
