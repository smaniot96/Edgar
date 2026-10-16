import { useId, useImperativeHandle, useLayoutEffect, useRef, type Ref } from "react";

import { Button } from "./ui/Button";

/**
 * Pinned action composer. Enter sends (Shift+Enter = newline; ignored while an IME is
 * composing). While a turn is running the textarea stays editable so the player can draft
 * their next move, and the Send button becomes Stop.
 */
export function ComposerBar({
  value,
  onChange,
  onSend,
  onStop,
  busy,
  canStop = true,
  allowSend = true,
  disabled = false,
  inputRef,
}: {
  value: string;
  onChange: (v: string) => void;
  onSend: () => void;
  onStop?: () => void;
  busy: boolean;
  canStop?: boolean;
  /** Keep editing enabled while a pending action must be reconciled before another send. */
  allowSend?: boolean;
  disabled?: boolean;
  inputRef?: Ref<HTMLTextAreaElement>;
}) {
  const id = useId();
  const hintId = `${id}-hint`;
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const canSend = allowSend && !busy && !disabled && value.trim().length > 0;
  useImperativeHandle(inputRef, () => textareaRef.current!);
  useLayoutEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    const minimum = 44;
    const maximum = 160;
    textarea.style.height = "auto";
    const height = Math.max(minimum, Math.min(textarea.scrollHeight, maximum));
    textarea.style.height = `${height}px`;
    textarea.style.overflowY = textarea.scrollHeight > maximum ? "auto" : "hidden";
  }, [value]);
  return (
    <footer className="shrink-0 border-t border-line bg-canvas/80 px-3 pt-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] backdrop-blur sm:px-4">
      <form
        className="mx-auto flex w-full max-w-3xl items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (canSend) onSend();
        }}
      >
        <label htmlFor={id} className="sr-only">
          What do you do?
        </label>
        <textarea
          id={id}
          className="max-h-40 min-h-[44px] flex-1 resize-none overflow-y-hidden rounded-control border border-line-strong bg-surface-sunken px-3 py-2.5 text-[0.95rem] leading-6 text-ink placeholder:text-ink-subtle focus:border-ember-600 focus:outline-none focus:ring-1 focus:ring-ember-500/70 disabled:cursor-not-allowed disabled:opacity-60"
          ref={textareaRef}
          rows={1}
          value={value}
          placeholder="What do you do? e.g. “I look around” or “I head to the tavern”"
          aria-describedby={hintId}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={(e) => {
            if (e.key !== "Enter" || e.shiftKey) return;
            // Don't hijack Enter while an IME (e.g. Japanese/Chinese input) is composing.
            if (e.nativeEvent.isComposing || e.keyCode === 229) return;
            e.preventDefault();
            if (canSend) onSend();
          }}
        />
        <span id={hintId} className="sr-only">
          Press Enter to send, Shift+Enter for a new line.
        </span>
        {busy && onStop ? (
          <Button
            variant="secondary"
            onClick={onStop}
            disabled={!canStop}
            title={canStop ? "Stop the DM" : "Saving — can't stop now"}
            className="h-11 shrink-0"
          >
            Stop
          </Button>
        ) : (
          <Button type="submit" disabled={!canSend} className="h-11 shrink-0">
            Send
          </Button>
        )}
      </form>
      <div className="mx-auto mt-1 flex w-full max-w-3xl items-center justify-between gap-3 px-1">
        <p className="text-[0.65rem] text-ink-subtle">Enter to send · Shift+Enter for a new line.</p>
        {value ? (
          <button
            type="button"
            disabled={busy}
            onClick={() => onChange("")}
            className="min-h-11 shrink-0 px-2 text-[0.7rem] font-medium text-ink-muted underline underline-offset-2 hover:text-ink disabled:cursor-not-allowed disabled:opacity-50"
          >
            Clear draft
          </button>
        ) : null}
      </div>
    </footer>
  );
}
