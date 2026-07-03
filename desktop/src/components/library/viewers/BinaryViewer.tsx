import { Archive, Download, FileText } from "lucide-react";

import { maybeAuthDownload } from "@/api/client";
import { useI18n } from "@/lib/i18n";

export function ArchiveView({ url, name }: { url: string; name: string }) {
  const { t } = useI18n();
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 p-6 text-center text-sm text-fg-muted">
      <Archive size={32} className="text-fg-subtle" />
      <p>{t.library.previewUnavailable}</p>
      <a href={url} download={name} onClick={(e) => maybeAuthDownload(e, url, name)}
         className="flex items-center gap-1 rounded-md border border-border bg-bg-subtle px-3 py-1.5 text-xs hover:bg-bg-muted">
        <Download size={12} /> {t.library.download}
      </a>
    </div>
  );
}
export function BinaryView({ url, name }: { url: string; name: string }) {
  const { t } = useI18n();
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 p-6 text-center text-sm text-fg-muted">
      <FileText size={32} className="text-fg-subtle" />
      <p>{t.library.previewUnavailable}</p>
      <a href={url} download={name} onClick={(e) => maybeAuthDownload(e, url, name)}
         className="flex items-center gap-1 rounded-md border border-border bg-bg-subtle px-3 py-1.5 text-xs hover:bg-bg-muted">
        <Download size={12} /> {t.library.download}
      </a>
    </div>
  );
}
