/** Right-side metadata drawer for the selected entry.
 *
 *  Collapsed by default to reserve real-estate for the viewer. Mirrors
 *  what `metadata` route returns (display_name, lifecycle, mime, summary,
 *  tags, related entries) but keeps the layout flexible — fields the
 *  backend hasn't filled yet are just hidden.
 */
import { ChevronRight, ChevronLeft, Tag, Sparkles, FileText } from "lucide-react";
import { Link } from "react-router-dom";

import type { FileMetadata } from "@/types/api";
import { cn } from "@/lib/utils";
import { useI18n, type I18nStrings } from "@/lib/i18n";

interface Props {
  meta: FileMetadata | null;
  loading: boolean;
  open: boolean;
  onToggle: () => void;
}

export function MetaPanel({ meta, loading, open, onToggle }: Props) {
  const { t } = useI18n();
  return (
    <aside className={cn(
      "flex shrink-0 flex-col border-l border-border bg-bg-subtle transition-[width] duration-150",
      open ? "w-72" : "w-8",
    )}>
      <button
        onClick={onToggle}
        title={open ? t.library.detailsHide : t.library.detailsShow}
        className="flex h-9 items-center justify-center border-b border-border text-fg-muted hover:bg-bg-muted"
      >
        {open ? <ChevronRight size={14} /> : <ChevronLeft size={14} />}
      </button>
      {open && (
        <div className="flex-1 overflow-y-auto p-3 text-xs">
          {loading && <p className="text-fg-subtle">{t.common.loading}</p>}
          {!loading && !meta && (
            <p className="text-fg-subtle">{t.library.metadataEmpty}</p>
          )}
          {meta && <MetaBody meta={meta} t={t} />}
        </div>
      )}
    </aside>
  );
}

function MetaBody({ meta, t }: { meta: FileMetadata; t: I18nStrings }) {
  return (
    <div className="space-y-3">
      <Field label={t.library.fields.name} value={meta.display_name} />
      <Field label={t.library.fields.lifecycle} value={meta.lifecycle} />
      {meta.mime_type && <Field label={t.library.fields.mime} value={meta.mime_type} mono />}
      {typeof meta.size_bytes === "number" && (
        <Field label={t.library.fields.size} value={formatBytes(meta.size_bytes)} />
      )}
      {meta.folder_path && <Field label={t.library.fields.folder} value={meta.folder_path} mono />}
      {meta.summary && (
        <section>
          <SectionHeader icon={<Sparkles size={11} />} text={t.library.fields.summary} />
          <p className="mt-1 whitespace-pre-wrap leading-relaxed text-fg-muted">
            {meta.summary}
          </p>
        </section>
      )}
      {meta.extra && (
        <section>
          <SectionHeader icon={<FileText size={11} />} text={t.library.fields.extra} />
          <p className="mt-1 whitespace-pre-wrap leading-relaxed text-fg-muted">
            {meta.extra}
          </p>
        </section>
      )}
      {meta.tags && meta.tags.length > 0 && (
        <section>
          <SectionHeader icon={<Tag size={11} />} text={t.library.fields.tags} />
          <div className="mt-1 flex flex-wrap gap-1">
            {meta.tags.map((t) => (
              <span key={t.name} className="rounded-md border border-border bg-bg-base px-1.5 py-0.5 text-[10px] text-fg-muted">
                {t.facet ? <span className="text-fg-subtle">{t.facet}:</span> : null}{t.name}
              </span>
            ))}
          </div>
        </section>
      )}
      {meta.related_entries && meta.related_entries.length > 0 && (
        <section>
          <SectionHeader text={t.library.fields.related} />
          <ul className="mt-1 space-y-1">
            {meta.related_entries.slice(0, 8).map((r) => (
              <li key={r.entry_id}>
                <Link to={`/library?entry=${r.entry_id}`}
                      className="block truncate text-accent hover:underline">
                  {r.display_name}
                </Link>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

function Field({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wide text-fg-subtle">{label}</div>
      <div className={cn("mt-0.5 break-words", mono && "font-mono text-[11px]")}>{value}</div>
    </div>
  );
}

function SectionHeader({ icon, text }: { icon?: React.ReactNode; text: string }) {
  return (
    <div className="flex items-center gap-1 text-[10px] uppercase tracking-wide text-fg-subtle">
      {icon}{text}
    </div>
  );
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}
