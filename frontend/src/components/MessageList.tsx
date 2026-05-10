import type { ChatMessageRead } from "../api/types";

import { cn } from "../lib/cn";

export interface Line {
  role: "user" | "dm";
  content: string;
}

export function MessageList({
  messages,
  streamingDm,
}: {
  messages: Line[] | ChatMessageRead[];
  streamingDm: string;
}) {
  return (
    <div
      className="flex min-h-0 flex-1 flex-col gap-1.5 overflow-y-auto p-4"
      aria-live="polite"
    >
      {messages.map((m, i) => (
        <div
          key={i}
          className={cn(
            "max-w-[720px] whitespace-pre-wrap break-words rounded-lg px-3 py-2.5",
            m.role === "user" ? "ml-auto bg-[#1f3a5f]" : "mr-auto bg-[#1f1f23]",
          )}
        >
          {m.content}
        </div>
      ))}
      {streamingDm ? (
        <div className="mr-auto max-w-[720px] whitespace-pre-wrap break-words rounded-lg bg-[#1f1f23] px-3 py-2.5">
          {streamingDm}
        </div>
      ) : null}
    </div>
  );
}
