import { useEffect, useRef } from "react";

import type { AdjudicationResult, CombatOutcome, DebugPayload } from "../api/types";

import { useMode } from "../dev/ModeContext";
import { DevPanel } from "../dev/DevPanel";
import { cn } from "../lib/cn";

export interface Line {
  role: "user" | "dm";
  content: string;
  /** Optional adjudication summary rendered as an inline roll chip on a DM message. */
  adjudication?: AdjudicationResult | null;
  /** Optional combat outcome banner shown when combat ended this turn. */
  combatOutcome?: CombatOutcome;
  /** Optional per-turn debug payload (captured only for turns sent in dev mode). */
  debug?: DebugPayload | null;
  /** True for DM turns produced while the turn stream was consumed (eligible for a dev panel). */
  isTurn?: boolean;
}

const OUTCOME_LABEL: Record<string, string> = {
  victory: "Victory",
  defeat: "Defeat",
  fled: "Fled",
};

const OUTCOME_STYLE: Record<string, string> = {
  victory: "bg-emerald-900/50 text-emerald-200 border-emerald-700",
  defeat: "bg-red-900/50 text-red-200 border-red-700",
  fled: "bg-amber-900/40 text-amber-200 border-amber-700",
};

function RollChip({ adj }: { adj: AdjudicationResult }) {
  const parts: string[] = [];
  if (typeof adj.dice_result === "number") parts.push(`🎲 ${adj.dice_result}`);
  if (typeof adj.damage === "number" && adj.damage > 0) parts.push(`${adj.damage} dmg`);
  return (
    <div className="mr-auto mt-1 flex max-w-[720px] flex-wrap items-center gap-2 text-xs">
      <span
        className={cn(
          "rounded-full border px-2 py-0.5 font-semibold",
          adj.success
            ? "border-emerald-700 bg-emerald-900/40 text-emerald-200"
            : "border-red-700 bg-red-900/40 text-red-200",
        )}
      >
        {adj.success ? "Success" : "Fail"}
      </span>
      {parts.map((p) => (
        <span key={p} className="rounded-full border border-[#333] bg-[#1a1c20] px-2 py-0.5 text-[#cfcfcf]">
          {p}
        </span>
      ))}
      {adj.mechanical_summary ? (
        <span className="text-[#9a9a9a]">{adj.mechanical_summary}</span>
      ) : null}
    </div>
  );
}

export function MessageList({
  messages,
  streamingDm,
}: {
  messages: Line[];
  streamingDm: string;
}) {
  const endRef = useRef<HTMLDivElement | null>(null);
  const { isDev } = useMode();

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [messages, streamingDm]);

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-1.5 overflow-y-auto p-4">
      {messages.map((m, i) => (
        <div key={i} className="flex flex-col gap-1">
          <div
            className={cn(
              "max-w-[720px] whitespace-pre-wrap break-words rounded-lg px-3 py-2.5",
              m.role === "user" ? "ml-auto bg-[#1f3a5f]" : "mr-auto bg-[#1f1f23]",
            )}
            aria-live={m.role === "dm" && i === messages.length - 1 ? "polite" : undefined}
          >
            {m.content}
          </div>
          {m.adjudication ? <RollChip adj={m.adjudication} /> : null}
          {isDev && m.role === "dm" && (m.isTurn || m.debug || m.adjudication) ? (
            <DevPanel debug={m.debug} adjudication={m.adjudication} />
          ) : null}
          {m.combatOutcome ? (
            <div
              className={cn(
                "mr-auto max-w-[720px] rounded-md border px-3 py-1.5 text-sm font-semibold",
                OUTCOME_STYLE[m.combatOutcome] ?? "border-[#333] bg-[#1f1f23] text-[#e6e6e6]",
              )}
            >
              {OUTCOME_LABEL[m.combatOutcome] ?? m.combatOutcome}
            </div>
          ) : null}
        </div>
      ))}
      {streamingDm ? (
        <div
          className="mr-auto max-w-[720px] whitespace-pre-wrap break-words rounded-lg bg-[#1f1f23] px-3 py-2.5"
          aria-live="polite"
        >
          {streamingDm}
        </div>
      ) : null}
      <div ref={endRef} />
    </div>
  );
}
