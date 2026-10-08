import { lazy, memo, Suspense, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import type { AdjudicationResult, DebugPayload } from "../api/types";
import { useMode } from "../dev/ModeContext";
import { cn } from "../lib/cn";
import { Narration } from "./play/Narration";
import { RollCard } from "./play/RollCard";
import { Button } from "./ui/Button";
import type { DmLine, ErrorLine, Line, PendingTurn } from "./play/turnState";

export type { Line } from "./play/turnState";

const DevPanel = lazy(() => import("../dev/DevPanel").then((m) => ({ default: m.DevPanel })));

/** Distance from the bottom (px) within which new content keeps the view pinned. */
const STICKY_THRESHOLD = 120;

const OUTCOME_LABEL: Record<string, string> = {
  victory: "Victory",
  defeat: "Defeat",
  fled: "Fled",
};

const OUTCOME_STYLE: Record<string, string> = {
  victory: "bg-success-soft text-success border-success/40",
  defeat: "bg-danger-soft text-danger border-danger-strong/60",
  fled: "bg-ember-950/50 text-ember-200 border-ember-800",
};

const THINKING_LABEL: Record<string, string> = {
  parsing: "The DM is considering your action…",
  retrieving: "The DM is consulting the tomes…",
  adjudicating: "The DM is rolling the dice…",
  narrating: "The DM is narrating…",
  saving: "The DM is recording the tale…",
};

function DevDetails({
  debug,
  adjudication,
}: {
  debug?: DebugPayload | null;
  adjudication?: AdjudicationResult | null;
}) {
  return (
    <Suspense fallback={<div className="mt-1 text-xs text-ink-subtle">Loading dev panel…</div>}>
      <DevPanel debug={debug} adjudication={adjudication} />
    </Suspense>
  );
}

function DmLabel() {
  return (
    <div
      aria-hidden
      className="mb-1 text-[0.65rem] font-semibold uppercase tracking-[0.2em] text-ember-500/80"
    >
      DM
    </div>
  );
}

const DmRow = memo(function DmRow({ line, isDev }: { line: DmLine; isDev: boolean }) {
  return (
    <article className="flex flex-col items-start gap-2" aria-label="Dungeon Master">
      <RollCard adj={line.adjudication} />
      <div className="w-full max-w-[65ch]">
        <DmLabel />
        <div
          className={cn(
            "break-words font-serif text-[1.05rem] leading-relaxed text-ink",
            line.interrupted && "opacity-80",
          )}
        >
          <Narration text={line.content} />
        </div>
        {line.interrupted ? (
          <p className="mt-1 text-xs italic text-ember-400/80">— narration interrupted (not saved)</p>
        ) : null}
      </div>
      {line.combatOutcome ? (
        <div
          className={cn(
            "rounded-md border px-3 py-1 text-sm font-semibold",
            OUTCOME_STYLE[line.combatOutcome] ?? "border-line-strong bg-surface text-ink",
          )}
        >
          Combat over · {OUTCOME_LABEL[line.combatOutcome] ?? line.combatOutcome}
        </div>
      ) : null}
      {isDev && (line.isTurn || line.debug || line.adjudication) ? (
        <div className="w-full max-w-[820px]">
          <DevDetails debug={line.debug} adjudication={line.adjudication} />
        </div>
      ) : null}
    </article>
  );
});

function ErrorRow({
  line,
  onRetry,
  canRetry,
}: {
  line: ErrorLine;
  onRetry?: (turnId: string, input: string) => void;
  canRetry: boolean;
}) {
  const retryable = !!(onRetry && line.turnId && line.retryInput);
  return (
    <div
      role="alert"
      className="flex max-w-[65ch] flex-wrap items-center gap-x-3 gap-y-2 rounded-control border border-danger-strong/60 bg-danger-soft/60 px-3 py-2 text-sm text-danger"
    >
      <span className="min-w-0 flex-1">{line.content}</span>
      {retryable ? (
        <Button
          variant="secondary"
          size="sm"
          disabled={!canRetry}
          onClick={() => onRetry?.(line.turnId as string, line.retryInput as string)}
        >
          Retry
        </Button>
      ) : null}
    </div>
  );
}

function Row({
  line,
  isDev,
  onRetry,
  canRetry,
}: {
  line: Line;
  isDev: boolean;
  onRetry?: (turnId: string, input: string) => void;
  canRetry: boolean;
}) {
  switch (line.role) {
    case "user":
      return (
        <div className="flex flex-col items-end gap-0.5">
          <div
            className={cn(
              "max-w-[min(80%,52ch)] whitespace-pre-wrap break-words rounded-2xl rounded-br-sm bg-surface-raised/70 px-3 py-1.5 text-sm text-ink",
              line.failed && "border border-dashed border-line-strong bg-transparent text-ink-muted",
            )}
          >
            <span className="sr-only">You: </span>
            {line.content}
          </div>
          {line.failed ? <span className="text-[0.7rem] text-ink-subtle">Not recorded</span> : null}
        </div>
      );
    case "dm":
      return <DmRow line={line} isDev={isDev} />;
    case "error":
      return <ErrorRow line={line} onRetry={onRetry} canRetry={canRetry} />;
    case "notice":
      return (
        <p className="mx-auto max-w-[65ch] text-center text-sm italic text-ink-muted">{line.content}</p>
      );
  }
}

/** Text the player sends to confirm the adventure is over (engine contract). */
const END_ADVENTURE_REPLY = "Yes, end the adventure";

function CompletionPrompt({ onReply, disabled }: { onReply?: (text: string) => void; disabled: boolean }) {
  return (
    <div className="flex max-w-[65ch] flex-wrap items-center gap-x-3 gap-y-2 rounded-control border border-ember-800/60 bg-ember-950/30 px-3 py-2 text-sm text-ink-muted">
      <span className="min-w-0 flex-1">
        The DM thinks the adventure may be over — reply “{END_ADVENTURE_REPLY}” to finish, or keep playing.
      </span>
      {onReply ? (
        <Button size="sm" disabled={disabled} onClick={() => onReply(END_ADVENTURE_REPLY)}>
          End the adventure
        </Button>
      ) : null}
    </div>
  );
}

function TypingIndicator({ stage }: { stage: string | null }) {
  return (
    <div className="flex items-center gap-2 text-sm italic text-ink-muted">
      <span aria-hidden className="flex gap-1">
        {[0, 150, 300].map((d) => (
          <span
            key={d}
            className="h-1.5 w-1.5 animate-bounce rounded-full bg-ember-500/80 motion-reduce:animate-none"
            style={{ animationDelay: `${d}ms` }}
          />
        ))}
      </span>
      <span>{(stage && THINKING_LABEL[stage]) || "The DM is thinking…"}</span>
    </div>
  );
}

function PendingRow({ pending }: { pending: PendingTurn }) {
  const hasText = pending.text.length > 0;
  return (
    <div className="flex flex-col items-start gap-2">
      <RollCard adj={pending.adjudication} />
      {hasText ? (
        <div className="w-full max-w-[65ch]">
          <DmLabel />
          <div className="break-words font-serif text-[1.05rem] leading-relaxed text-ink">
            <Narration text={pending.text} />
          </div>
        </div>
      ) : null}
      {!hasText || pending.stage === "saving" ? <TypingIndicator stage={pending.stage} /> : null}
    </div>
  );
}

export function MessageList({
  messages,
  pending,
  onRetry,
  canRetry = true,
  forceScrollKey,
  onQuickReply,
}: {
  messages: Line[];
  pending: PendingTurn | null;
  onRetry?: (turnId: string, input: string) => void;
  canRetry?: boolean;
  /** Changing this (e.g. a new turn id) scrolls to the bottom regardless of position. */
  forceScrollKey?: string | null;
  /** Send a canned reply (e.g. confirming the end of the adventure). Omit to hide such prompts. */
  onQuickReply?: (text: string) => void;
}) {
  const { isDev } = useMode();
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const nearBottomRef = useRef(true);
  const [showJump, setShowJump] = useState(false);

  const last = messages[messages.length - 1];
  const showCompletionPrompt =
    !pending && !!onQuickReply && last?.role === "dm" && last.adjudication?.completion_status === "pending";

  const scrollToBottom = useCallback((smooth = false) => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior: smooth ? "smooth" : "auto" });
    nearBottomRef.current = true;
    setShowJump(false);
  }, []);

  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const near = el.scrollHeight - el.scrollTop - el.clientHeight <= STICKY_THRESHOLD;
    nearBottomRef.current = near;
    setShowJump(!near);
  }, []);

  // Follow new content only while the reader is already at the bottom.
  const pendingText = pending?.text;
  const pendingStage = pending?.stage;
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    if (nearBottomRef.current) {
      el.scrollTop = el.scrollHeight;
    } else if (el.scrollHeight - el.scrollTop - el.clientHeight > STICKY_THRESHOLD) {
      setShowJump(true);
    }
  }, [messages, pendingText, pendingStage]);

  useEffect(() => {
    if (forceScrollKey) scrollToBottom();
  }, [forceScrollKey, scrollToBottom]);

  return (
    <div className="relative min-h-0 flex-1">
      <div
        ref={scrollRef}
        onScroll={onScroll}
        // `relative` makes the scroller the containing block for absolutely positioned descendants
        // (sr-only labels); otherwise they escape the scroll clip and stretch the whole page.
        className="relative h-full overflow-y-auto overscroll-contain"
        aria-label="Adventure log"
        role="log"
        aria-live="off"
      >
        <div className="mx-auto flex w-full max-w-3xl flex-col gap-5 px-4 py-6 sm:px-6">
          {messages.length === 0 && !pending ? (
            <p className="text-center text-sm italic text-ink-subtle">Your adventure is about to begin…</p>
          ) : null}
          {messages.map((m) => (
            <Row key={m.id} line={m} isDev={isDev} onRetry={onRetry} canRetry={canRetry} />
          ))}
          {showCompletionPrompt ? (
            <CompletionPrompt onReply={onQuickReply} disabled={!canRetry} />
          ) : null}
          {pending ? <PendingRow pending={pending} /> : null}
        </div>
      </div>
      {showJump ? (
        <button
          type="button"
          onClick={() => scrollToBottom(true)}
          className="absolute bottom-3 left-1/2 -translate-x-1/2 rounded-full border border-ember-700/70 bg-surface/95 px-3 py-1 text-xs font-semibold text-ember-200 shadow-lg shadow-black/40 hover:bg-surface-raised focus-visible:outline focus-visible:outline-2 focus-visible:outline-ember-400"
        >
          ↓ Jump to latest
        </button>
      ) : null}
    </div>
  );
}
