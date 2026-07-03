/** External-link handling for the Tauri shell.
 *
 *  Tauri 2 webviews drop `target="_blank"` navigations and `window.open()`
 *  unless an opener plugin handles them, so plain anchors to https://...
 *  silently do nothing in the packaged app. Anchor onClick handlers call
 *  `interceptExternalLink` to route the URL through tauri-plugin-opener's
 *  `openUrl` (system browser) instead; in a regular browser it is a no-op
 *  and the default `target="_blank"` behavior is kept.
 */
import { describeError, frontendLog } from "@/lib/frontendLog";

function isTauri(): boolean {
  if (typeof window === "undefined") return false;
  return Boolean(
    (window as unknown as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ ||
      (window as unknown as { __TAURI__?: unknown }).__TAURI__,
  );
}

function isExternalUrl(url: string): boolean {
  return /^https?:\/\//i.test(url) || /^mailto:/i.test(url);
}

export function interceptExternalLink(
  e: { preventDefault: () => void },
  href: string | undefined,
): void {
  if (!href || !isTauri() || !isExternalUrl(href)) return;
  e.preventDefault();
  void import("@tauri-apps/plugin-opener")
    .then(({ openUrl }) => openUrl(href))
    .catch((err) => {
      frontendLog("error", "failed to open external URL", {
        url: href,
        error: describeError(err),
      });
    });
}
