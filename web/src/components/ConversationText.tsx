import type { ReactNode } from "react";

function inline(text: string): ReactNode[] {
  return text.split(/(\*\*[^*\n]+\*\*|`[^`\n]+`)/).map((part, index) =>
    part.startsWith("**") && part.endsWith("**") ? <strong key={index}>{part.slice(2, -2)}</strong>
      : part.startsWith("`") && part.endsWith("`") ? <code key={index}>{part.slice(1, -1)}</code> : part);
}

/** A small safe renderer for conversational paragraphs, headings and lists. */
export function ConversationText({ text }: { text: string }) {
  const blocks: ReactNode[] = [];
  const lines = text.split("\n");
  let paragraph: string[] = [];
  let items: string[] = [];
  let ordered = false;
  const flushParagraph = () => {
    if (paragraph.length) blocks.push(<p key={blocks.length}>{inline(paragraph.join("\n"))}</p>);
    paragraph = [];
  };
  const flushList = () => {
    if (items.length) {
      const children = items.map((item, index) => <li key={index}>{inline(item)}</li>);
      blocks.push(ordered ? <ol key={blocks.length}>{children}</ol> : <ul key={blocks.length}>{children}</ul>);
    }
    items = [];
  };
  for (const line of lines) {
    const bullet = /^\s*[-*]\s+(.+)$/.exec(line);
    const numbered = /^\s*\d+[.)]\s+(.+)$/.exec(line);
    const heading = /^#{1,4}\s+(.+)$/.exec(line);
    if (bullet || numbered) {
      flushParagraph();
      const nextOrdered = Boolean(numbered);
      if (items.length && ordered !== nextOrdered) flushList();
      ordered = nextOrdered;
      items.push((bullet ?? numbered)![1]);
    } else {
      flushList();
      if (heading) { flushParagraph(); blocks.push(<h4 key={blocks.length}>{inline(heading[1])}</h4>); }
      else if (!line.trim()) flushParagraph();
      else paragraph.push(line);
    }
  }
  flushParagraph(); flushList();
  return <div className="conversation-text">{blocks}</div>;
}
