import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

const API_PROXY_TARGET =
  process.env.VITE_API_TARGET || "http://127.0.0.1:8000";

// Vite is the dev server for both browser-mode and Tauri-mode (Tauri's
// webview points at this URL). The proxy lets the frontend issue
// same-origin /v1/* requests during dev, sidestepping CORS preflight
// for the common case; CORS is still configured server-side for the
// less common cross-origin Tauri/production deploys.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "src") },
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/v1": {
        target: API_PROXY_TARGET,
        changeOrigin: true,
      },
      "/health": {
        target: API_PROXY_TARGET,
        changeOrigin: true,
      },
    },
  },
  clearScreen: false,
  envPrefix: ["VITE_", "TAURI_"],
});
