import { memo } from "react";
import Markdown, { type Components } from "react-markdown";

const components: Components = {
  p: ({ children }) => <p className="mb-3 last:mb-0">{children}</p>,
  strong: ({ children }) => <strong className="font-semibold text-ember-100">{children}</strong>,
  em: ({ children }) => <em className="italic">{children}</em>,
  h1: ({ children }) => <h3 className="mb-2 mt-4 text-lg font-semibold text-ember-200 first:mt-0">{children}</h3>,
  h2: ({ children }) => <h3 className="mb-2 mt-4 text-lg font-semibold text-ember-200 first:mt-0">{children}</h3>,
  h3: ({ children }) => <h4 className="mb-2 mt-3 font-semibold text-ember-200 first:mt-0">{children}</h4>,
  h4: ({ children }) => <h4 className="mb-2 mt-3 font-semibold text-ember-200 first:mt-0">{children}</h4>,
  ul: ({ children }) => <ul className="mb-3 list-disc space-y-1 pl-5 marker:text-ember-500/70 last:mb-0">{children}</ul>,
  ol: ({ children }) => <ol className="mb-3 list-decimal space-y-1 pl-5 marker:text-ember-500/70 last:mb-0">{children}</ol>,
  li: ({ children }) => <li className="pl-0.5">{children}</li>,
  blockquote: ({ children }) => (
    <blockquote className="mb-3 border-l-2 border-ember-700/60 pl-3 italic text-ink last:mb-0">
      {children}
    </blockquote>
  ),
  hr: () => <hr className="my-4 border-line-strong" />,
  code: ({ children }) => (
    <code className="rounded bg-surface-raised px-1 py-px font-mono text-[0.85em] text-ink">{children}</code>
  ),
  pre: ({ children }) => (
    <pre className="mb-3 overflow-x-auto rounded bg-surface p-2 font-mono text-sm last:mb-0">{children}</pre>
  ),
  a: ({ children, href }) => (
    <a href={href} target="_blank" rel="noopener noreferrer" className="text-ember-300 underline underline-offset-2 hover:text-ember-200">
      {children}
    </a>
  ),
  img: () => null,
};

/** DM narration rendered as markdown (raw HTML is dropped, images ignored). */
export const Narration = memo(function Narration({ text }: { text: string }) {
  return (
    <Markdown skipHtml components={components}>
      {text}
    </Markdown>
  );
});
