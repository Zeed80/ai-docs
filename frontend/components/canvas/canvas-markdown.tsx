"use client";

interface CanvasMarkdownProps {
  content: string;
}

function parseInline(text: string): string {
  return text
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\*(.+?)\*/g, "<em>$1</em>")
    .replace(
      /`(.+?)`/g,
      '<code class="bg-slate-700 px-1 rounded text-sm font-mono">$1</code>',
    )
    .replace(
      /\[(.+?)\]\((.+?)\)/g,
      '<a href="$2" class="text-blue-400 underline" target="_blank" rel="noopener">$1</a>',
    );
}

export function CanvasMarkdown({ content }: CanvasMarkdownProps) {
  const lines = content.split("\n");
  const elements: React.ReactNode[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    // Heading
    const h = line.match(/^(#{1,6})\s+(.+)/);
    if (h) {
      const level = h[1].length;
      const text = parseInline(h[2]);
      const cls =
        level === 1
          ? "text-xl font-bold mt-4 mb-2"
          : level === 2
            ? "text-lg font-semibold mt-3 mb-1"
            : "text-base font-semibold mt-2 mb-1";
      elements.push(
        <div
          key={i}
          className={cls}
          dangerouslySetInnerHTML={{ __html: text }}
        />,
      );
      i++;
      continue;
    }

    // HR
    if (/^---+$/.test(line.trim())) {
      elements.push(<hr key={i} className="border-slate-600 my-3" />);
      i++;
      continue;
    }

    // Unordered list
    if (/^[-*+]\s/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^[-*+]\s/.test(lines[i])) {
        items.push(lines[i].replace(/^[-*+]\s/, ""));
        i++;
      }
      elements.push(
        <ul key={`ul-${i}`} className="list-disc list-inside space-y-0.5 my-1">
          {items.map((item, j) => (
            <li
              key={j}
              dangerouslySetInnerHTML={{ __html: parseInline(item) }}
            />
          ))}
        </ul>,
      );
      continue;
    }

    // Ordered list
    if (/^\d+\.\s/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\d+\.\s/.test(lines[i])) {
        items.push(lines[i].replace(/^\d+\.\s/, ""));
        i++;
      }
      elements.push(
        <ol
          key={`ol-${i}`}
          className="list-decimal list-inside space-y-0.5 my-1"
        >
          {items.map((item, j) => (
            <li
              key={j}
              dangerouslySetInnerHTML={{ __html: parseInline(item) }}
            />
          ))}
        </ol>,
      );
      continue;
    }

    // Code block
    if (line.startsWith("```")) {
      const codeLines: string[] = [];
      i++;
      while (i < lines.length && !lines[i].startsWith("```")) {
        codeLines.push(lines[i]);
        i++;
      }
      i++;
      elements.push(
        <pre
          key={`code-${i}`}
          className="bg-slate-900 border border-slate-700 rounded p-3 overflow-x-auto text-sm font-mono my-2"
        >
          <code>{codeLines.join("\n")}</code>
        </pre>,
      );
      continue;
    }

    // Blank line
    if (line.trim() === "") {
      i++;
      continue;
    }

    // Paragraph
    elements.push(
      <p
        key={i}
        className="my-1 leading-relaxed"
        dangerouslySetInnerHTML={{ __html: parseInline(line) }}
      />,
    );
    i++;
  }

  return (
    <div className="text-slate-200 text-sm leading-relaxed">{elements}</div>
  );
}
