import { useEffect } from "react";
import { Routes, Route, Navigate } from "react-router-dom";

import { BackendGate } from "@/components/BackendGate";
import { Sidebar } from "@/components/Sidebar";
import { TopBar } from "@/components/TopBar";
import { StatusBar } from "@/components/StatusBar";
import { LibraryPage } from "@/pages/LibraryPage";
import { ChatPage } from "@/pages/ChatPage";
import { SearchPage } from "@/pages/SearchPage";
import { SettingsPage } from "@/pages/SettingsPage";
import { HelpPage } from "@/pages/HelpPage";
import { AboutPage } from "@/pages/AboutPage";
import { useI18n } from "@/lib/i18n";
import { useTheme } from "@/lib/theme";

function isTauri(): boolean {
  return Boolean(
    (window as unknown as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ ||
      (window as unknown as { __TAURI__?: unknown }).__TAURI__,
  );
}

export default function App() {
  const initTheme = useTheme((s) => s.init);
  const { locale } = useI18n();

  useEffect(() => {
    return initTheme();
  }, [initTheme]);

  // The tray menu lives in the Rust shell, which has no locale of its
  // own — push the resolved UI language over so its items match.
  useEffect(() => {
    if (!isTauri()) return;
    void import("@tauri-apps/api/core")
      .then(({ invoke }) => invoke("set_ui_language", { lang: locale }))
      .catch(() => { /* older shells without the command */ });
  }, [locale]);

  return (
    <BackendGate>
      <div className="flex h-full w-full flex-col bg-bg-base text-fg-base">
        <div className="flex min-h-0 flex-1 overflow-hidden">
          <Sidebar />
          <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
            <TopBar />
            <main className="min-h-0 flex-1 overflow-hidden">
              <Routes>
                <Route path="/" element={<Navigate to="/chat" replace />} />
                <Route path="/library/*" element={<LibraryPage />} />
                <Route path="/chat" element={<ChatPage />} />
                <Route path="/search" element={<SearchPage />} />
                <Route path="/settings" element={<SettingsPage />} />
                <Route path="/help" element={<HelpPage />} />
                <Route path="/about" element={<AboutPage />} />
                {/* Safety net: stray hash fragments (e.g. an unresolved
                    in-answer "#foo" anchor) must not blank the pane. */}
                <Route path="*" element={<Navigate to="/chat" replace />} />
              </Routes>
            </main>
          </div>
        </div>
        <StatusBar />
      </div>
    </BackendGate>
  );
}
