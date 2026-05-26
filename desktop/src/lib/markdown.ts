/** Convert LaTeX bracket math (`\(…\)` and `\[…\]`) to dollar-delimited
 *  math (`$…$` and `$$…$$`) before remark-math sees it. remark-math has
 *  no first-class support for the bracket form.
 *
 *  Lifted from cherry-studio (src/renderer/src/utils/markdown.ts). The
 *  algorithm protects code spans/fences and link targets first so we
 *  don't molest `\[ref]` style links or backticked `\(x\)` examples,
 *  then walks balanced bracket pairs respecting odd/even backslash
 *  escapes.
 */

const CONTAINS_LATEX = /\\\(.*?\\\)|\\\[.*?\\\]/s;
const PLACEHOLDER = "__MARGINALIA_LATEX_PROTECTED_";

interface BracketMatch {
  pre: string;
  body: string;
  post: string;
}

function findLatexMatch(
  text: string,
  open: string,
  close: string,
): BracketMatch | null {
  const escaped = (i: number) => {
    let count = 0;
    while (--i >= 0 && text[i] === "\\") count++;
    return count & 1;
  };

  for (let i = 0, n = text.length; i <= n - open.length; i++) {
    if (!text.startsWith(open, i) || escaped(i)) continue;
    for (let j = i + open.length, depth = 1; j <= n - close.length && depth; j++) {
      const delta =
        text.startsWith(open, j) && !escaped(j) ? 1
        : text.startsWith(close, j) && !escaped(j) ? -1
        : 0;
      if (delta) {
        depth += delta;
        if (!depth) {
          return {
            pre: text.slice(0, i),
            body: text.slice(i + open.length, j),
            post: text.slice(j + close.length),
          };
        }
        j += (delta > 0 ? open : close).length - 1;
      }
    }
  }
  return null;
}

function processMath(content: string, open: string, close: string, wrap: string): string {
  let result = "";
  let remaining = content;
  while (remaining.length > 0) {
    const m = findLatexMatch(remaining, open, close);
    if (!m) {
      result += remaining;
      break;
    }
    result += m.pre + wrap + m.body + wrap;
    remaining = m.post;
  }
  return result;
}

export function processLatexBrackets(text: string): string {
  if (!CONTAINS_LATEX.test(text)) return text;

  const protectedItems: string[] = [];
  let processed = text
    .replace(/(```[\s\S]*?```|`[^`]*`)/g, (m) => {
      const i = protectedItems.length;
      protectedItems.push(m);
      return `${PLACEHOLDER}${i}__`;
    })
    .replace(/\[([^[\]]*(?:\[[^\]]*\][^[\]]*)*)\]\([^)]*?\)/g, (m) => {
      const i = protectedItems.length;
      protectedItems.push(m);
      return `${PLACEHOLDER}${i}__`;
    });

  let out = processMath(processed, "\\[", "\\]", "$$");
  out = processMath(out, "\\(", "\\)", "$");

  const restoreRe = new RegExp(`${PLACEHOLDER}(\\d+)__`, "g");
  out = out.replace(restoreRe, (m, idxStr) => {
    const idx = parseInt(idxStr, 10);
    return idx >= 0 && idx < protectedItems.length ? protectedItems[idx] : m;
  });
  return out;
}
