/** Per-profile LLM editor.
 *
 *  Each row is one of chat/reflect/ingest/vision/audio. The form starts
 *  prefilled with whatever is in the overlay (so unset fields stay
 *  blank — the placeholder shows the inherited default). Save sends a
 *  PATCH with only the changed fields; clearing a field to empty sends
 *  `null` so the override is removed and the profile falls back to the
 *  default.
 *
 *  The api_key field is special: we never receive the raw value (server
 *  masks it), so the input shows a placeholder telling the user a key
 *  is set. Typing replaces it; leaving it blank keeps whatever's
 *  already configured. */
import { useEffect, useMemo, useState } from "react";
import { Save, RotateCcw, Loader2 } from "lucide-react";

import { settings as settingsApi } from "@/api/client";
import { cn } from "@/lib/utils";
import type { LlmProfileName, LlmSettings } from "@/types/api";

const PROFILES: LlmProfileName[] = ["chat", "reflect", "ingest", "vision", "audio"];

type FormState = Partial<Record<string, string>>;

interface Props {
  data: LlmSettings;
  onChange: (next: LlmSettings) => void;
}

export function LlmProfileEditor({ data, onChange }: Props) {
  const [open, setOpen] = useState<LlmProfileName | null>(null);

  return (
    <div className="space-y-2">
      {PROFILES.map((name) => (
        <ProfileRow
          key={name}
          name={name}
          data={data}
          isOpen={open === name}
          onToggle={() => setOpen(open === name ? null : name)}
          onChange={onChange}
        />
      ))}
    </div>
  );
}

interface RowProps {
  name: LlmProfileName;
  data: LlmSettings;
  isOpen: boolean;
  onToggle: () => void;
  onChange: (next: LlmSettings) => void;
}

function ProfileRow({ name, data, isOpen, onToggle, onChange }: RowProps) {
  const profile = data.profiles[name];
  const overlay = data.overlay;
  const overlayKey = (suffix: string) => `llm_${name}_${suffix}`;

  const [form, setForm] = useState<FormState>({});
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<number | null>(null);

  useEffect(() => {
    setForm({
      provider: (overlay[overlayKey("provider")] as string) ?? "",
      model: (overlay[overlayKey("model")] as string) ?? "",
      base_url: (overlay[overlayKey("base_url")] as string) ?? "",
      api_key: "",
    });
    setErr(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [name, isOpen]);

  const dirty = useMemo(() => {
    return (
      (form.provider ?? "") !== (overlay[overlayKey("provider")] ?? "") ||
      (form.model ?? "") !== (overlay[overlayKey("model")] ?? "") ||
      (form.base_url ?? "") !== (overlay[overlayKey("base_url")] ?? "") ||
      (form.api_key ?? "") !== ""
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [form, overlay, name]);

  const overrideCount = ["provider", "model", "base_url", "api_key"].filter(
    (k) => overlay[overlayKey(k)] != null,
  ).length;

  const save = async () => {
    setSaving(true);
    setErr(null);
    try {
      const patch: Record<string, string | null> = {};
      for (const k of ["provider", "model", "base_url", "api_key"] as const) {
        const v = form[k];
        if (v === undefined) continue;
        if (k === "api_key" && v === "") continue;
        if (v === "") patch[overlayKey(k)] = null;
        else patch[overlayKey(k)] = v;
      }
      if (Object.keys(patch).length === 0) {
        setSaving(false);
        return;
      }
      const next = await settingsApi.updateLlm(patch);
      onChange(next);
      setSavedAt(Date.now());
      setForm((f) => ({ ...f, api_key: "" }));
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const reset = async () => {
    setSaving(true);
    setErr(null);
    try {
      const patch: Record<string, null> = {};
      for (const k of ["provider", "model", "base_url", "api_key"] as const) {
        patch[overlayKey(k)] = null;
      }
      const next = await settingsApi.updateLlm(patch);
      onChange(next);
      setForm({ provider: "", model: "", base_url: "", api_key: "" });
      setSavedAt(Date.now());
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="rounded-md border border-border bg-bg-base">
      <button
        onClick={onToggle}
        className="flex w-full items-center justify-between px-3 py-2 text-left hover:bg-bg-subtle"
      >
        <div className="flex items-center gap-3">
          <span className="font-medium capitalize">{name}</span>
          <span className="font-mono text-xs text-fg-subtle">
            {profile.provider}/{profile.model || "(unset)"}
          </span>
        </div>
        <span className="text-xs text-fg-subtle">
          {overrideCount > 0
            ? `${overrideCount} override${overrideCount > 1 ? "s" : ""}`
            : "inherited"}
        </span>
      </button>

      {isOpen && (
        <div className="space-y-3 border-t border-border px-3 py-3 text-sm">
          <Field label="Provider">
            <select
              value={form.provider ?? ""}
              onChange={(e) => setForm({ ...form, provider: e.target.value })}
              className="w-full rounded border border-border bg-bg-base px-2 py-1 text-sm"
            >
              <option value="">(inherit: {data.defaults.provider})</option>
              <option value="openai">openai</option>
              <option value="openai-compatible">openai-compatible</option>
              <option value="anthropic">anthropic</option>
            </select>
          </Field>
          <Field label="Model">
            <input
              value={form.model ?? ""}
              onChange={(e) => setForm({ ...form, model: e.target.value })}
              placeholder={`(inherit: ${data.defaults.model})`}
              className="w-full rounded border border-border bg-bg-base px-2 py-1 font-mono text-sm"
            />
          </Field>
          <Field label="Base URL">
            <input
              value={form.base_url ?? ""}
              onChange={(e) => setForm({ ...form, base_url: e.target.value })}
              placeholder={data.defaults.base_url || "(provider default)"}
              className="w-full rounded border border-border bg-bg-base px-2 py-1 font-mono text-sm"
            />
          </Field>
          <Field label="API key">
            <input
              type="password"
              value={form.api_key ?? ""}
              onChange={(e) => setForm({ ...form, api_key: e.target.value })}
              placeholder={
                profile.api_key_set
                  ? `(set: ${profile.api_key ?? ""})`
                  : "(unset)"
              }
              className="w-full rounded border border-border bg-bg-base px-2 py-1 font-mono text-sm"
            />
            <p className="mt-1 text-xs text-fg-subtle">
              Leave blank to keep the existing key. Type a new key to replace.
            </p>
          </Field>

          {err && (
            <p className="rounded bg-danger/10 px-2 py-1 text-xs text-danger">
              {err}
            </p>
          )}

          <div className="flex items-center justify-between pt-1">
            <button
              onClick={reset}
              disabled={saving || overrideCount === 0}
              className="flex items-center gap-1 text-xs text-fg-subtle hover:text-fg-base disabled:opacity-40"
            >
              <RotateCcw size={11} /> Reset to default
            </button>
            <button
              onClick={save}
              disabled={!dirty || saving}
              className={cn(
                "flex items-center gap-1.5 rounded bg-accent px-3 py-1 text-xs font-medium text-accent-fg",
                "hover:opacity-90 disabled:opacity-40",
              )}
            >
              {saving ? <Loader2 size={11} className="animate-spin" /> : <Save size={11} />}
              Save
            </button>
          </div>
          {savedAt && !saving && (
            <p className="text-right text-xs text-fg-subtle">
              Saved · {new Date(savedAt).toLocaleTimeString()}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs font-medium text-fg-muted">{label}</span>
      {children}
    </label>
  );
}
