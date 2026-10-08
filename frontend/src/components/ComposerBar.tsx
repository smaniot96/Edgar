import { useId, type Ref } from "react";

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
  disabled = false,
  inputRef,
}: {
  value: string;
  onChange: (v: string) => void;
  onSend: () => void;
  onStop?: () => void;
  busy: boolean;
  canStop?: boolean;
  disabled?: boolean;
  inputRef?: Ref<HTMLTextAreaElement>;
}) {
  const id = useId();
  const hintId = `${id}-hint`;
  const canSend = !busy && !disabled && value.trim().length > 0;
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
          ref={inputRef}
          className="max-h-40 min-h-[44px] flex-1 resize-none rounded-control border border-line-strong bg-surface-sunken px-3 py-2.5 text-[0.95rem] text-ink placeholder:text-ink-subtle focus:border-ember-600 focus:outline-none focus:ring-1 focus:ring-ember-500/70 disabled:cursor-not-allowed disabled:opacity-60"
          rows={2}
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
    </footer>
  );
}
