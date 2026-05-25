import { useState } from "react";
import { Save } from "lucide-react";

import { setBaseUrl, getBaseUrl } from "@/api/client";
import { cn } from "@/lib/utils";

const STORAGE_KEY = "marginalia.api_base";

export function SettingsPage() {
  const [base, setBase] = useState(
    () => localStorage.getItem(STORAGE_KEY) || getBaseUrl(),
  );
  const [savedAt, setSavedAt] = useState<number | null>(null);

  const save = () => {
    const v = base.trim().replace(/\/$/, "");
    if (v) localStorage.setItem(STORAGE_KEY, v);
    else localStorage.removeItem(STORAGE_KEY);
    setBaseUrl(v);
    setSavedAt(Date.now());
  };

  return (
    <div className="h-full overflow-y-auto px-8 py-8">
      <div className="mx-auto max-w-2xl">
        <h1 className="text-xl font-semibold">Settings</h1>
        <p className="mt-1 text-sm text-fg-muted">
          Connection to the Marginalia backend.
        </p>

        <section className="mt-6 rounded-md border border-border bg-bg-subtle p-4">
          <label className="block text-sm font-medium">API base URL</label>
          <p className="mt-1 text-xs text-fg-subtle">
            Leave empty to use the dev proxy (recommended in browser).
            Set to <span className="font-mono">http://host:8000</span> when
            connecting to a remote server.
          </p>
          <div className="mt-3 flex gap-2">
            <input
              value={base}
              onChange={(e) => setBase(e.target.value)}
              placeholder="(empty = same-origin / proxy)"
              className="flex-1 rounded-md border border-border bg-bg-base px-3 py-1.5 text-sm font-mono outline-none focus:border-accent"
            />
            <button
              onClick={save}
              className={cn(
                "flex items-center gap-1.5 rounded-md bg-accent px-3 text-sm font-medium text-accent-fg hover:opacity-90",
              )}
            >
              <Save size={13} /> Save
            </button>
          </div>
          {savedAt && (
            <p className="mt-2 text-xs text-fg-subtle">
              Saved · {new Date(savedAt).toLocaleTimeString()}
            </p>
          )}
        </section>

        <section className="mt-6 rounded-md border border-border bg-bg-subtle p-4 text-sm text-fg-muted">
          <h2 className="mb-2 text-sm font-medium text-fg-base">More settings — coming soon</h2>
          <ul className="space-y-1 text-xs">
            <li>· LLM provider / model selection</li>
            <li>· Storage backend (mirror / local / s3)</li>
            <li>· Conflict policy (rename / error / skip)</li>
            <li>· Tend / dispatcher controls</li>
          </ul>
        </section>
      </div>
    </div>
  );
}
