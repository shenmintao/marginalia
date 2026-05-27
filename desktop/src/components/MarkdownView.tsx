/** Shared markdown renderer used by chat answers, Library .md preview,
 *  and anywhere else we need rich body text.
 *
 *  Borrows from cherry-studio's Markdown component:
 *    - remark-gfm           tables, task lists, footnotes, autolinks
 *    - remark-cjk-friendly  proper spacing around CJK + Latin/numerics
 *    - remark-math          $inline$ and $$display$$
 *    - rehype-katex         KaTeX renders the math nodes
 *    - remark-github-blockquote-alert   `> [!NOTE]` style admonitions
 *
 *  Code blocks use react-syntax-highlighter with a copy button overlay.
 *  An optional `onEntryLink` callback intercepts `entry:<uuid>` URLs
 *  (citation footnotes from the agent) and routes them in-app instead
 *  of letting the browser try to open a custom-scheme URL.
 */
import "katex/dist/katex.min.css";

import { Check, Copy } from "lucide-react";
import ReactMarkdown from "react-markdown";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { vscDarkPlus, prism } from "react-syntax-highlighter/dist/esm/styles/prism";
import remarkGfm from "remark-gfm";
import remarkCjkFriendly from "remark-cjk-friendly";
import remarkMath from "remark-math";
import remarkAlert from "remark-github-blockquote-alert";
import rehypeKatex from "rehype-katex";
import type { Pluggable } from "unified";

import { useTheme } from "@/lib/theme";
import { processLatexBrackets } from "@/lib/markdown";
import { useTemporaryValue } from "@/hooks/useTemporaryValue";
import { cn } from "@/lib/utils";

/** Deep-link position on top of an `entry:<uuid>` citation. runtime.py
 *  rewrites the agent's footnote definitions into one of three URL shapes:
 *    text / md / docx:   ?q=<urlencoded verbatim excerpt>
 *    PDF:                ?page=<n>
 *    legacy / manual:    ?line=<start>-<end>  (still tolerated)
 *  The receiving handler maps these onto FileViewer's matching jump modes. */
export interface EntryLocator {
  kind: "quote" | "line" | "page";
  value: string;
}

function parseEntryHref(href: string): { id: string; locator?: EntryLocator } {
  const tail = href.slice("entry:".length);
  const q = tail.indexOf("?");
  if (q === -1) return { id: tail };
  const id = tail.slice(0, q);
  const params = new URLSearchParams(tail.slice(q + 1));
  const quote = params.get("q");
  if (quote) return { id, locator: { kind: "quote", value: quote } };
  const line = params.get("line");
  if (line) return { id, locator: { kind: "line", value: line } };
  const page = params.get("page");
  if (page) return { id, locator: { kind: "page", value: page } };
  return { id };
}

interface Props {
  content: string;
  /** Called when the user clicks an `entry:<uuid>` link (citation
   *  footnote in chat answers). `locator` carries the optional deep-link
   *  position from the URL query string — `?q=<text>` yields
   *  `{kind: "quote", value}`, `?line=10-40` yields `{kind: "line", value}`,
   *  `?page=3` yields `{kind: "page", value}`. Handlers route to
   *  /library?entry=... with the locator forwarded so the file viewer
   *  can scroll to that position. If absent, entry: links render as
   *  plain anchors. */
  onEntryLink?: (entryId: string, locator?: EntryLocator) => void;
  /** Tailwind class for the wrapping div. Defaults to `prose-marginalia`. */
  className?: string;
  /** Override the `clobberPrefix` mdast-util-to-hast applies to footnote
   *  ids (default `user-content-`). When several MarkdownView instances
   *  render on the same page (the chat transcript stacks one per turn),
   *  every turn would otherwise emit `user-content-fn-a`, `user-content-
   *  fnref-a`, … and the browser scrolls to the first matching id —
   *  which is in turn 1 regardless of which turn the user clicked.
   *  Pass a per-turn prefix (e.g. `user-content-<conversationId>-`) so
   *  each turn lives in its own id namespace. */
  idPrefix?: string;
}

const REMARK_PLUGINS: Pluggable[] = [
  remarkGfm,
  remarkCjkFriendly,
  remarkMath,
  remarkAlert,
];
const REHYPE_PLUGINS: Pluggable[] = [rehypeKatex];

// react-markdown v9 strips href values whose scheme isn't in its safe-list
// (http/https/mailto/tel/...). Without this hook, `entry:<uuid>` citation
// links arrive at our `a` renderer with href="" — the browser then treats
// an empty href as "reload the current page", which is the symptom the
// user reported (clicking a citation just opens /chat again).
function safeUrl(url: string): string {
  if (url.startsWith("entry:")) return url;
  // Defer everything else to react-markdown's default sanitisation by
  // returning the url as-is for the schemes the library already accepts;
  // other schemes get stripped. We re-implement the default safelist
  // (http/https/mailto/tel/irc/ircs/xmpp + relative) inline because
  // react-markdown doesn't export it.
  if (/^(https?|mailto|tel|irc|ircs|xmpp):/i.test(url)) return url;
  if (url.startsWith("/") || url.startsWith("#") || url.startsWith("?")) return url;
  if (!/^[a-z][a-z0-9+.-]*:/i.test(url)) return url; // relative path
  return "";
}

export function MarkdownView({ content, onEntryLink, className, idPrefix }: Props) {
  const renderLink = ({
    href, children, ...rest
  }: React.AnchorHTMLAttributes<HTMLAnchorElement> & {
    children?: React.ReactNode;
  }) => {
    if (onEntryLink && href && href.startsWith("entry:")) {
      const { id, locator } = parseEntryHref(href);
      return (
        <a
          href="#"
          onClick={(e) => { e.preventDefault(); onEntryLink(id, locator); }}
          className="text-accent hover:underline"
          {...rest}
        >
          {children}
        </a>
      );
    }
    // In-page anchors (GFM footnote jumps `[^a]` → `#user-content-fn-a`,
    // and the matching back-arrow ↩ from the footnote def) must not get
    // `target="_blank"` — that opens a fresh tab on /chat#... instead of
    // scrolling within the current view. We also intercept the click and
    // use `block: "nearest"` so the scroll container only moves the
    // minimum amount needed to expose the target. `block: "center"`
    // would pull the footnote to the middle of the pane — when the
    // footnote already sits near the bottom of the answer, that pushes
    // half a viewport of empty space underneath it.
    if (href && href.startsWith("#")) {
      return (
        <a
          href={href}
          onClick={(e) => {
            const id = decodeURIComponent(href.slice(1));
            const el = document.getElementById(id);
            if (!el) return;
            e.preventDefault();
            el.scrollIntoView({ behavior: "smooth", block: "nearest" });
          }}
          {...rest}
        >
          {children}
        </a>
      );
    }
    return (
      <a href={href} target="_blank" rel="noreferrer" {...rest}>
        {children}
      </a>
    );
  };

  return (
    <div className={cn("prose-marginalia", className)}>
      <ReactMarkdown
        remarkPlugins={REMARK_PLUGINS}
        rehypePlugins={REHYPE_PLUGINS}
        remarkRehypeOptions={idPrefix ? { clobberPrefix: idPrefix } : undefined}
        urlTransform={safeUrl}
        components={{
          a: renderLink,
          code: CodeRenderer,
          pre: ({ children }) => <>{children}</>,
        }}
      >
        {processLatexBrackets(content)}
      </ReactMarkdown>
    </div>
  );
}

/** react-markdown passes both inline and fenced code through this
 *  component. Inline `code` has no language class; fenced blocks have
 *  `language-<lang>` set by remark. We render fenced blocks with prism
 *  + a copy button, and leave inline as a plain styled <code>. */
function CodeRenderer({
  className, children, ...rest
}: React.HTMLAttributes<HTMLElement> & { children?: React.ReactNode }) {
  const match = /language-([\w-+]+)/.exec(className || "");
  const text = String(children ?? "").replace(/\n$/, "");
  const isFenced = !!match || text.includes("\n");

  if (!isFenced) {
    return <code className={className} {...rest}>{children}</code>;
  }
  return <CodeBlock language={match?.[1] ?? "text"} text={text} />;
}

function CodeBlock({ language, text }: { language: string; text: string }) {
  const { effective } = useTheme();
  const [copied, setCopied] = useTemporaryValue(false, 1500);
  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch { /* ignore — non-secure context, clipboard blocked, etc. */ }
  };
  return (
    <div className="group relative my-3 overflow-hidden rounded-lg border border-border bg-bg-muted">
      <div className="flex items-center justify-between border-b border-border bg-bg-subtle px-3 py-1 text-[11px] text-fg-subtle">
        <span className="font-mono">{language}</span>
        <button
          onClick={onCopy}
          className="flex items-center gap-1 rounded px-1.5 py-0.5 hover:bg-bg-muted hover:text-fg-base"
          title="Copy to clipboard"
        >
          {copied
            ? <><Check size={11} /> copied</>
            : <><Copy size={11} /> copy</>}
        </button>
      </div>
      <SyntaxHighlighter
        language={language}
        style={effective === "dark" ? vscDarkPlus : prism}
        customStyle={{
          margin: 0,
          padding: "10px 14px",
          fontSize: 12,
          background: "transparent",
        }}
        codeTagProps={{ style: { fontFamily: "inherit" } }}
        wrapLongLines={false}
      >
        {text}
      </SyntaxHighlighter>
    </div>
  );
}
