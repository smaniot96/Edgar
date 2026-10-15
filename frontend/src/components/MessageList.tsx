import { lazy, memo, Suspense, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import type { AdjudicationResult, DebugPayload } from "../api/types";
import { useMode } from "../dev/ModeContext";
import { cn } from "../lib/cn";
import { Narration } from "./play/Narration";
import { RollCard } from "./play/RollCard";
import { Button } from "./ui/Button";
import type { DmLine, ErrorLine, Line, PendingTurn } from "./play/turnState";

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
    <article
      id={line.eventId != null ? `story-event-${line.eventId}` : undefined}
      tabIndex={line.eventId != null ? -1 : undefined}
      data-event-id={line.eventId}
      className="flex flex-col items-start gap-2 outline-none focus-visible:ring-2 focus-visible:ring-ember-500/70"
      aria-label="Dungeon Master"
    >
      {line.rollStatus === "not_captured" ? (
        <p className="text-xs italic text-ink-subtle" data-roll-status="not_captured">
          Roll details were not captured for this turn.
        </p>
      ) : (
        <RollCard adj={line.adjudication} />
      )}
      <div className="w-full max-w-[65ch]">
        <DmLabel />
        <div
          className={cn(
            "break-words font-serif text-[1.05rem] leading-relaxed text-ink",
            line.interrupted && "opacity-80",
          )}
        >
          <Narration text={line.content} />
          {line.citations?.length ? <details className="mt-2 font-sans text-xs text-ink-muted">
            <summary>Source references</summary>
            <p>References supplied for this turn; ruling evidence was explicitly selected by the adjudicator.</p>
            <ul className="mt-1 space-y-1">{line.citations.map((ref) => <li key={ref.reference_id}>
              {ref.source} · edition {ref.rules_edition} · pages {ref.source_pages.join(", ") || "unknown"} · {ref.edition_status}{line.ruling_citation_ids?.includes(ref.reference_id) ? " · ruling evidence" : ""}
              <details><summary>Version</summary><p className="break-all">Source: {ref.source_sha256 ?? "unknown"}<br />Extraction: {ref.extraction_sha256 ?? "unknown"}</p></details>
            </li>)}</ul>
          </details> : null}
        </div>
        {line.interrupted ? (
          <p className="mt-1 text-xs italic text-ember-400/80">
            {line.uncertain ? "— narration interrupted; save not confirmed" : "— narration interrupted (not saved)"}
          </p>
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
  onCheckReceipt,
  canRetry,
}: {
  line: ErrorLine;
  onRetry?: (turnId: string, input: string, requestId?: string, draftRevision?: number) => void;
  onCheckReceipt?: (turnId: string, requestId: string, input: string, draftRevision?: number) => void;
  canRetry: boolean;
}) {
  const retryable = !!(onRetry && line.turnId && line.retryInput && line.retryRequestId && !line.uncertain);
  const checkable = !!(onCheckReceipt && line.turnId && line.retryInput && line.retryRequestId && line.uncertain);
  return (
    <div
      role="alert"
      className="flex max-w-[65ch] flex-wrap items-center gap-x-3 gap-y-2 rounded-control border border-danger-strong/60 bg-danger-soft/60 px-3 py-2 text-sm text-danger"
    >
      <span className="min-w-0 flex-1">{line.content}</span>
      {checkable ? (
        <Button
          variant="secondary"
          size="sm"
          disabled={!canRetry}
          onClick={() => onCheckReceipt?.(line.turnId as string, line.retryRequestId as string, line.retryInput as string, line.draftRevision)}
        >
          Check save status
        </Button>
      ) : null}
      {retryable ? (
        <Button
          variant="secondary"
          size="sm"
          disabled={!canRetry}
          onClick={() => onRetry?.(line.turnId as string, line.retryInput as string, line.retryRequestId, line.draftRevision)}
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
  onCheckReceipt,
  canRetry,
}: {
  line: Line;
  isDev: boolean;
  onRetry?: (turnId: string, input: string, requestId?: string, draftRevision?: number) => void;
  onCheckReceipt?: (turnId: string, requestId: string, input: string, draftRevision?: number) => void;
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
          {line.failed ? <span className="text-[0.7rem] text-ink-subtle">{line.uncertain ? "Save not confirmed" : "Not recorded"}</span> : null}
        </div>
      );
    case "dm":
      return <DmRow line={line} isDev={isDev} />;
    case "error":
      return <ErrorRow line={line} onRetry={onRetry} onCheckReceipt={onCheckReceipt} canRetry={canRetry} />;
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
  onCheckReceipt,
  canRetry = true,
  layoutKey,
  storageKey,
  restoreReady = true,
  openEventId,
  onHistoryOpened,
  onQuickReply,
}: {
  messages: Line[];
  pending: PendingTurn | null;
  onRetry?: (turnId: string, input: string, requestId?: string, draftRevision?: number) => void;
  onCheckReceipt?: (turnId: string, requestId: string, input: string, draftRevision?: number) => void;
  canRetry?: boolean;
  /** Changes when a tool opens or the available story width changes. */
  layoutKey?: string | number;
  /** Session-scoped storage key for restoring the reader's current history anchor. */
  storageKey?: string;
  /** Wait until the boot transcript is seeded before applying the saved anchor. */
  restoreReady?: boolean;
  /** Stable narration anchor selected from the verified journal. */
  openEventId?: number | null;
  onHistoryOpened?: (eventId: number) => void;
  /** Send a canned reply (e.g. confirming the end of the adventure). Omit to hide such prompts. */
  onQuickReply?: (text: string) => void;
}) {
  const { isDev } = useMode();
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const nearBottomRef = useRef(true);
  const anchorRef = useRef<{ messageId: string | null; eventId: number | null; offset: number; scrollTop: number } | null>(null);
  const restoredRef = useRef(false);
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

  const findAnchor = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const top = el.getBoundingClientRect().top;
    const rows = el.querySelectorAll<HTMLElement>("[data-message-id]");
    for (const row of rows) {
      const rect = row.getBoundingClientRect();
      if (rect.bottom > top + 1) {
        const event = row.dataset.eventId;
        return {
          messageId: row.dataset.messageId || null,
          eventId: event && /^\d+$/.test(event) ? Number(event) : null,
          offset: rect.top - top,
          scrollTop: el.scrollTop,
        };
      }
    }
    return { messageId: null, eventId: null, offset: 0, scrollTop: el.scrollTop };
  }, []);

  const storeAnchor = useCallback((anchor: NonNullable<typeof anchorRef.current>) => {
    anchorRef.current = anchor;
    if (!storageKey) return;
    try {
      sessionStorage.setItem(storageKey, JSON.stringify(anchor));
    } catch {
      /* Storage is best-effort; the live view still keeps its anchor in memory. */
    }
  }, [storageKey]);

  const restoreAnchor = useCallback(() => {
    const el = scrollRef.current;
    const anchor = anchorRef.current;
    if (!el || !anchor) return;
    const row = anchor.eventId != null
      ? el.querySelector<HTMLElement>(`[data-event-id="${anchor.eventId}"]`)
      : anchor.messageId
        ? Array.from(el.querySelectorAll<HTMLElement>("[data-message-id]")).find((candidate) => candidate.dataset.messageId === anchor.messageId)
        : null;
    if (row) {
      el.scrollTop += row.getBoundingClientRect().top - el.getBoundingClientRect().top - anchor.offset;
    } else {
      el.scrollTop = Math.min(anchor.scrollTop, el.scrollHeight - el.clientHeight);
    }
    nearBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight <= STICKY_THRESHOLD;
    setShowJump(!nearBottomRef.current);
  }, []);

  const onScroll = useCallback(() => {
    if (!restoreReady) return;
    const el = scrollRef.current;
    if (!el) return;
    const near = el.scrollHeight - el.scrollTop - el.clientHeight <= STICKY_THRESHOLD;
    nearBottomRef.current = near;
    setShowJump(!near);
    const anchor = findAnchor();
    if (anchor) storeAnchor(anchor);
  }, [findAnchor, storeAnchor, restoreReady]);

  const openHistory = useCallback((eventId: number) => {
    const scroller = scrollRef.current;
    if (!scroller) return;
    const target = document.getElementById(`story-event-${eventId}`);
    if (!target || !scroller.contains(target)) return;
    scroller.scrollTop += target.getBoundingClientRect().top - scroller.getBoundingClientRect().top - 12;
    target.focus({ preventScroll: true });
    nearBottomRef.current = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight <= STICKY_THRESHOLD;
    setShowJump(!nearBottomRef.current);
    const anchor = findAnchor();
    if (anchor) storeAnchor(anchor);
  }, [findAnchor, storeAnchor]);

  useLayoutEffect(() => {
    if (openEventId == null) return;
    openHistory(openEventId);
    onHistoryOpened?.(openEventId);
  }, [openEventId, openHistory, onHistoryOpened]);

  // Restore a session's last visible line after route navigation or a full reload.
  useLayoutEffect(() => {
    if (!restoreReady || restoredRef.current) return;
    if (storageKey) {
      try {
        const raw = sessionStorage.getItem(storageKey);
        if (raw) {
          const parsed = JSON.parse(raw) as typeof anchorRef.current;
          if (parsed && typeof parsed.scrollTop === "number" && typeof parsed.offset === "number") {
            anchorRef.current = parsed;
            restoreAnchor();
            restoredRef.current = true;
            return;
          }
        }
      } catch {
        /* Ignore a malformed or unavailable session-scoped anchor. */
      }
    }
    restoredRef.current = true;
    if (nearBottomRef.current) {
      const anchor = findAnchor();
      if (anchor) storeAnchor(anchor);
    }
  }, [messages, storageKey, findAnchor, restoreAnchor, storeAnchor, restoreReady]);

  // Follow new content only while the reader is already at the bottom.
  const pendingText = pending?.text;
  const pendingStage = pending?.stage;
  useLayoutEffect(() => {
    if (!restoreReady) return;
    const el = scrollRef.current;
    if (!el) return;
    if (nearBottomRef.current) {
      el.scrollTop = el.scrollHeight;
      const anchor = findAnchor();
      if (anchor) storeAnchor(anchor);
    } else if (el.scrollHeight - el.scrollTop - el.clientHeight > STICKY_THRESHOLD) {
      setShowJump(true);
    }
  }, [messages, pendingText, pendingStage, findAnchor, storeAnchor, restoreReady]);

  // Preserve a stable visible line when the side tool changes width or the viewport resizes.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      if (nearBottomRef.current) {
        el.scrollTop = el.scrollHeight;
      } else {
        restoreAnchor();
      }
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, [restoreAnchor]);

  useLayoutEffect(() => {
    if (!restoredRef.current || nearBottomRef.current) return;
    restoreAnchor();
  }, [layoutKey, restoreAnchor]);

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
            <div key={m.id} data-message-id={m.id} data-event-id={m.role === "dm" ? m.eventId : undefined}>
              <Row line={m} isDev={isDev} onRetry={onRetry} onCheckReceipt={onCheckReceipt} canRetry={canRetry} />
            </div>
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
          onClick={() => scrollToBottom(!(typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches))}
          className="absolute bottom-3 left-1/2 -translate-x-1/2 rounded-full border border-ember-700/70 bg-surface/95 px-3 py-1 text-xs font-semibold text-ember-200 shadow-lg shadow-black/40 hover:bg-surface-raised focus-visible:outline focus-visible:outline-2 focus-visible:outline-ember-400"
        >
          ↓ Jump to latest
        </button>
      ) : null}
    </div>
  );
}
