import { useEffect } from "react";
import { Routes, Route, Navigate } from "react-router-dom";

import { Sidebar } from "@/components/Sidebar";
import { TopBar } from "@/components/TopBar";
import { StatusBar } from "@/components/StatusBar";
import { LibraryPage } from "@/pages/LibraryPage";
import { ChatPage } from "@/pages/ChatPage";
import { SearchPage } from "@/pages/SearchPage";
import { SettingsPage } from "@/pages/SettingsPage";
import { useTheme } from "@/lib/theme";

export default function App() {
  const initTheme = useTheme((s) => s.init);

  useEffect(() => {
    return initTheme();
  }, [initTheme]);

  return (
    <div className="flex h-full w-full flex-col bg-bg-base text-fg-base">
      <div className="flex flex-1 overflow-hidden">
        <Sidebar />
        <div className="flex flex-1 flex-col overflow-hidden">
          <TopBar />
          <main className="flex-1 overflow-hidden">
            <Routes>
              <Route path="/" element={<Navigate to="/chat" replace />} />
              <Route path="/library/*" element={<LibraryPage />} />
              <Route path="/chat" element={<ChatPage />} />
              <Route path="/search" element={<SearchPage />} />
              <Route path="/settings" element={<SettingsPage />} />
            </Routes>
          </main>
        </div>
      </div>
      <StatusBar />
    </div>
  );
}
