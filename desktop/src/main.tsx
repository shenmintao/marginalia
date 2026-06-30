import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, HashRouter } from "react-router-dom";

import App from "./App";
import { resolveTauriBaseUrl } from "./api/client";
import { frontendLog, installFrontendErrorLogging } from "./lib/frontendLog";
import "./styles/globals.css";

installFrontendErrorLogging();

// Kick off Tauri backend-port resolution before render. The fetch wrapper
// awaits this on first use, so a slow IPC round-trip just delays the first
// API call rather than blocking the window from showing.
void resolveTauriBaseUrl().then((baseUrl) => {
  frontendLog("info", "initial backend base URL resolved", { baseUrl });
});

function isTauri(): boolean {
  return Boolean(
    (window as unknown as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ ||
      (window as unknown as { __TAURI__?: unknown }).__TAURI__,
  );
}

const Router = isTauri() ? HashRouter : BrowserRouter;

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Router>
      <App />
    </Router>
  </StrictMode>,
);
