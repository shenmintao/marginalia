import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import App from "./App";
import { resolveTauriBaseUrl } from "./api/client";
import "./styles/globals.css";

// Kick off Tauri backend-port resolution before render. The fetch wrapper
// awaits this on first use, so a slow IPC round-trip just delays the first
// API call rather than blocking the window from showing.
void resolveTauriBaseUrl();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </StrictMode>,
);
