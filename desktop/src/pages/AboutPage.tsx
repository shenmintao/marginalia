import { useState } from "react";
import { AlertCircle, CheckCircle2, ExternalLink, RefreshCw } from "lucide-react";

import { APP_VERSION } from "@/lib/appVersion";
import { useI18n } from "@/lib/i18n";
import { interceptExternalLink } from "@/lib/openExternal";
import { cn } from "@/lib/utils";

const RELEASES_URL = "https://github.com/shenmintao/marginalia/releases";
const LATEST_RELEASE_API =
  "https://api.github.com/repos/shenmintao/marginalia/releases/latest";

interface LatestRelease {
  tag_name?: string;
  name?: string;
  html_url?: string;
  published_at?: string;
}

type CheckState =
  | { status: "idle" }
  | { status: "checking" }
  | { status: "ok"; release: LatestRelease; updateAvailable: boolean }
  | { status: "error"; message: string };

export function AboutPage() {
  const { t } = useI18n();
  const [state, setState] = useState<CheckState>({ status: "idle" });

  const checkLatest = async () => {
    setState({ status: "checking" });
    try {
      const res = await fetch(LATEST_RELEASE_API, {
        headers: { Accept: "application/vnd.github+json" },
      });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      const release = (await res.json()) as LatestRelease;
      const latest = normalizeVersion(release.tag_name || release.name || "");
      if (!latest) throw new Error(t.about.latestInvalid);
      setState({
        status: "ok",
        release,
        updateAvailable: compareVersions(latest, normalizeVersion(APP_VERSION)) > 0,
      });
    } catch (e: unknown) {
      setState({
        status: "error",
        message: e instanceof Error ? e.message : String(e),
      });
    }
  };

  return (
    <div className="h-full overflow-y-auto px-8 py-8">
      <div className="mx-auto max-w-3xl space-y-6">
        <header>
          <h1 className="text-xl font-semibold">{t.about.title}</h1>
          <p className="mt-1 text-sm text-fg-muted">{t.about.subtitle}</p>
        </header>

        <section className="rounded-md border border-border bg-bg-subtle p-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="text-sm font-semibold">{t.about.versionTitle}</h2>
              <p className="mt-1 font-mono text-lg">v{APP_VERSION}</p>
            </div>
            <button
              type="button"
              disabled={state.status === "checking"}
              onClick={() => void checkLatest()}
              className={cn(
                "inline-flex items-center gap-2 rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-accent-fg",
                "hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-60",
              )}
            >
              <RefreshCw
                className={cn("h-4 w-4", state.status === "checking" && "animate-spin")}
              />
              {state.status === "checking"
                ? t.about.checkingLatest
                : t.about.checkLatest}
            </button>
          </div>

          <LatestVersionResult state={state} />
        </section>

        <section className="rounded-md border border-border bg-bg-subtle p-4">
          <h2 className="text-sm font-semibold">{t.about.linksTitle}</h2>
          <div className="mt-3 grid gap-2 sm:grid-cols-2">
            <ExternalLinkButton href="https://github.com/shenmintao/marginalia">
              {t.about.projectHomepage}
            </ExternalLinkButton>
            <ExternalLinkButton href={RELEASES_URL}>
              {t.about.downloadReleases}
            </ExternalLinkButton>
            <ExternalLinkButton href="https://github.com/shenmintao/marginalia/issues">
              {t.about.reportIssue}
            </ExternalLinkButton>
            <ExternalLinkButton href="https://github.com/shenmintao/marginalia/blob/main/LICENSE">
              {t.about.license}
            </ExternalLinkButton>
          </div>
        </section>

        <section className="rounded-md border border-border bg-bg-subtle p-4">
          <h2 className="text-sm font-semibold">{t.about.privacyTitle}</h2>
          <p className="mt-2 text-sm leading-6 text-fg-muted">
            {t.about.privacyBody}
          </p>
        </section>
      </div>
    </div>
  );
}

function LatestVersionResult({ state }: { state: CheckState }) {
  const { t, localeTag } = useI18n();
  if (state.status === "idle" || state.status === "checking") {
    return (
      <p className="mt-3 text-sm text-fg-subtle">{t.about.latestIdle}</p>
    );
  }
  if (state.status === "error") {
    return (
      <div className="mt-3 flex items-start gap-2 rounded-md bg-danger/10 px-3 py-2 text-sm">
        <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-danger" />
        <div>
          <div className="font-medium text-fg-base">{t.about.latestFailed}</div>
          <p className="mt-0.5 text-fg-muted">{state.message}</p>
        </div>
      </div>
    );
  }

  const tag = state.release.tag_name || state.release.name || "";
  const published = state.release.published_at
    ? new Date(state.release.published_at).toLocaleDateString(localeTag)
    : null;

  return (
    <div className="mt-3 flex items-start gap-2 rounded-md bg-bg-base px-3 py-2 text-sm">
      <CheckCircle2
        className={cn(
          "mt-0.5 h-4 w-4 shrink-0",
          state.updateAvailable ? "text-accent" : "text-fg-muted",
        )}
      />
      <div>
        <div className="font-medium text-fg-base">
          {state.updateAvailable
            ? t.about.updateAvailable(tag)
            : t.about.upToDate(tag)}
        </div>
        {published && (
          <p className="mt-0.5 text-fg-muted">{t.about.publishedAt(published)}</p>
        )}
        <a
          href={state.release.html_url || RELEASES_URL}
          target="_blank"
          rel="noreferrer"
          onClick={(e) => interceptExternalLink(e, state.release.html_url || RELEASES_URL)}
          className="mt-2 inline-flex items-center gap-1 text-xs text-accent hover:underline"
        >
          {t.about.openLatestRelease}
          <ExternalLink className="h-3 w-3" />
        </a>
      </div>
    </div>
  );
}

function ExternalLinkButton({
  href,
  children,
}: {
  href: string;
  children: React.ReactNode;
}) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      onClick={(e) => interceptExternalLink(e, href)}
      className="inline-flex items-center justify-between gap-2 rounded-md border border-border bg-bg-base px-3 py-2 text-sm text-fg-muted hover:bg-bg-muted hover:text-fg-base"
    >
      <span>{children}</span>
      <ExternalLink className="h-3.5 w-3.5 shrink-0" />
    </a>
  );
}

function normalizeVersion(value: string): string {
  return value.trim().replace(/^v/i, "").split(/[+-]/, 1)[0];
}

function compareVersions(a: string, b: string): number {
  const pa = parseVersionParts(a);
  const pb = parseVersionParts(b);
  for (let i = 0; i < Math.max(pa.length, pb.length); i += 1) {
    const av = pa[i] ?? 0;
    const bv = pb[i] ?? 0;
    if (av !== bv) return av > bv ? 1 : -1;
  }
  return 0;
}

function parseVersionParts(value: string): number[] {
  return value
    .split(".")
    .map((part) => parseInt(part.replace(/\D.*$/, ""), 10))
    .map((part) => (Number.isFinite(part) ? part : 0));
}
