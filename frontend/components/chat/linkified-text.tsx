import type { ReactNode } from "react";

// [text](url) where url is absolute or an app path, or a bare http(s) address.
const LINK_RE =
  /\[([^\]\n]+)\]\(((?:https?:\/\/|\/)[^\s)]+)\)|(https?:\/\/[^\s<>"«»]+)/g;
// Sentence punctuation glued to a bare address is not part of it.
const TRAILING_RE = /[.,;:!?)\]'"»]+$/;

/**
 * Agent text with its links clickable; each opens in a new browser tab so the
 * chat stays where it was. Only http(s) and app paths become links: any other
 * scheme (javascript:, data:) stays plain text.
 */
export function LinkifiedText({ text }: { text: string }) {
  const parts: ReactNode[] = [];
  let last = 0;
  for (const match of text.matchAll(LINK_RE)) {
    const start = match.index ?? 0;
    let href: string;
    let label: string;
    let consumed = match[0].length;
    if (match[2]) {
      href = match[2];
      label = match[1];
    } else {
      const raw = match[3];
      const tail = raw.match(TRAILING_RE)?.[0] ?? "";
      href = raw.slice(0, raw.length - tail.length);
      label = href;
      consumed = href.length;
    }
    if (start > last) parts.push(text.slice(last, start));
    parts.push(
      <a
        key={start}
        href={href}
        target="_blank"
        rel="noopener noreferrer"
        className="break-all text-sky-300 underline underline-offset-2 hover:text-sky-200"
      >
        {label}
      </a>,
    );
    last = start + consumed;
  }
  if (last < text.length) parts.push(text.slice(last));
  return <>{parts}</>;
}
