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

import { useState } from "react";
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
import { cn } from "@/lib/utils";

interface Props {
  content: string;
  /** Called when the user clicks an `entry:<uuid>` link (citation
   *  footnote in chat answers). The handler typically navigates to
   *  /library?entry=<id>. If absent, entry: links render as plain
   *  anchors and the browser handles them. */
  onEntryLink?: (entryId: string) => void;
  /** Tailwind class for the wrapping div. Defaults to `prose-marginalia`. */
  className?: string;
}

const REMARK_PLUGINS: Pluggable[] = [
  remarkGfm,
  remarkCjkFriendly,
  remarkMath,
  remarkAlert,
];
const REHYPE_PLUGINS: Pluggable[] = [rehypeKatex];

export function MarkdownView({ content, onEntryLink, className }: Props) {
  const renderLink = ({
    href, children, ...rest
  }: React.AnchorHTMLAttributes<HTMLAnchorElement> & {
    children?: React.ReactNode;
  }) => {
    if (onEntryLink && href && href.startsWith("entry:")) {
      const id = href.slice("entry:".length);
      return (
        <a
          href="#"
          onClick={(e) => { e.preventDefault(); onEntryLink(id); }}
          className="text-accent hover:underline"
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
        components={{
          a: renderLink,
          code: CodeRenderer,
          pre: ({ children }) => <>{children}</>,
        }}
      >
        {content}
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
  const [copied, setCopied] = useState(false);
  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
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
