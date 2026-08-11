import { Fragment, type ReactNode } from "react";

// Renders the small markdown subset agent replies use (bold, italic, inline
// code, fenced code, lists, headings) as React elements — no HTML injection.
// Underscores are never emphasis: file_names_with_underscores are common here.

const INLINE_TOKEN = /(\*\*[^*]+\*\*|\*[^*\s](?:[^*]*[^*\s])?\*|`[^`]+`)/g;

function renderInline(text: string): ReactNode[] {
  return text.split(INLINE_TOKEN).map((part, index) => {
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
      return <strong key={index}>{part.slice(2, -2)}</strong>;
    }
    if (part.startsWith("`") && part.endsWith("`") && part.length > 2) {
      return <code key={index}>{part.slice(1, -1)}</code>;
    }
    if (part.startsWith("*") && part.endsWith("*") && part.length > 2) {
      return <em key={index}>{part.slice(1, -1)}</em>;
    }
    return <Fragment key={index}>{part}</Fragment>;
  });
}

type Block =
  | { kind: "p"; lines: string[] }
  | { kind: "list"; ordered: boolean; items: string[] }
  | { kind: "pre"; text: string }
  | { kind: "heading"; text: string };

const FENCE = /^\s*```/;
const BULLET = /^\s*[-*]\s+(.*)$/;
const NUMBERED = /^\s*\d+[.)]\s+(.*)$/;
const HEADING = /^#{1,6}\s+(.*)$/;

function parseBlocks(text: string): Block[] {
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  const blocks: Block[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.trim() === "") {
      i += 1;
      continue;
    }
    if (FENCE.test(line)) {
      const buffer: string[] = [];
      i += 1;
      while (i < lines.length && !FENCE.test(lines[i])) {
        buffer.push(lines[i]);
        i += 1;
      }
      i += 1; // closing fence
      blocks.push({ kind: "pre", text: buffer.join("\n") });
      continue;
    }
    const listPattern = BULLET.test(line)
      ? BULLET
      : NUMBERED.test(line)
        ? NUMBERED
        : null;
    if (listPattern) {
      const items: string[] = [];
      while (i < lines.length && listPattern.test(lines[i])) {
        items.push(lines[i].match(listPattern)![1]);
        i += 1;
      }
      blocks.push({ kind: "list", ordered: listPattern === NUMBERED, items });
      continue;
    }
    const heading = line.match(HEADING);
    if (heading) {
      blocks.push({ kind: "heading", text: heading[1] });
      i += 1;
      continue;
    }
    const buffer: string[] = [line];
    i += 1;
    while (
      i < lines.length &&
      lines[i].trim() !== "" &&
      !FENCE.test(lines[i]) &&
      !BULLET.test(lines[i]) &&
      !NUMBERED.test(lines[i]) &&
      !HEADING.test(lines[i])
    ) {
      buffer.push(lines[i]);
      i += 1;
    }
    blocks.push({ kind: "p", lines: buffer });
  }
  return blocks;
}

export function Markdown({ text }: { text: string }) {
  return (
    <div className="md">
      {parseBlocks(text).map((block, index) => {
        switch (block.kind) {
          case "pre":
            return (
              <pre key={index}>
                <code>{block.text}</code>
              </pre>
            );
          case "list": {
            const items = block.items.map((item, itemIndex) => (
              <li key={itemIndex}>{renderInline(item)}</li>
            ));
            return block.ordered ? (
              <ol key={index}>{items}</ol>
            ) : (
              <ul key={index}>{items}</ul>
            );
          }
          case "heading":
            // Kept as a bold line: real h1–h6 would be oversized in a chat
            // bubble and out of place in the page's heading outline.
            return (
              <p key={index} className="md-heading">
                {renderInline(block.text)}
              </p>
            );
          case "p":
            return (
              <p key={index}>
                {block.lines.map((line, lineIndex) => (
                  <Fragment key={lineIndex}>
                    {lineIndex > 0 && <br />}
                    {renderInline(line)}
                  </Fragment>
                ))}
              </p>
            );
        }
      })}
    </div>
  );
}
