import { useEffect, useId, useRef, type KeyboardEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";

import { Button } from "./Button";
import { ErrorNotice } from "./ErrorNotice";
import type { ButtonVariant } from "./styles";

const FOCUSABLE =
  'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * In-app replacement for `window.confirm` (EDG-055 F15): embedded browsers suppress native
 * dialogs, which made confirmed actions silently do nothing.
 *
 * A modal alertdialog: the rest of the page is inert, Tab stays inside, Escape or Cancel closes
 * it, and focus returns to the control that opened it. Cancel has initial focus so Enter never
 * confirms a destructive action by accident. While `busy` the dialog stays open and cannot be
 * dismissed; a failure is shown inside it so the player can retry or cancel.
 */
export function ConfirmDialog({
  open,
  title,
  children,
  confirmLabel,
  busyLabel,
  cancelLabel = "Cancel",
  variant = "danger",
  busy = false,
  error = null,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  /** Consequence text: name exactly what is changed or deleted. */
  children: ReactNode;
  confirmLabel: string;
  busyLabel?: string;
  cancelLabel?: string;
  variant?: ButtonVariant;
  busy?: boolean;
  error?: unknown;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const titleId = useId();
  const bodyId = useId();
  const panel = useRef<HTMLDivElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    // Everything except this dialog's portal becomes inert, so neither pointer, keyboard nor
    // assistive technology can reach the page behind it.
    const host = panel.current?.closest("[data-confirm-dialog]");
    const hidden = [...document.body.children].filter(
      (el): el is HTMLElement => el instanceof HTMLElement && el !== host && !el.inert,
    );
    for (const el of hidden) el.inert = true;
    cancel.current?.focus();
    return () => {
      for (const el of hidden) el.inert = false;
      if (opener?.isConnected) opener.focus();
    };
  }, [open]);

  // Both buttons are disabled while busy, which drops focus out of the dialog; bring it back
  // so Escape and Tab keep working once the action settles (e.g. to read an error and retry).
  useEffect(() => {
    if (open && !busy && !panel.current?.contains(document.activeElement)) cancel.current?.focus();
  }, [open, busy]);

  if (!open) return null;

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      if (!busy) onCancel();
      return;
    }
    if (event.key !== "Tab" || !panel.current) return;
    const items = [...panel.current.querySelectorAll<HTMLElement>(FOCUSABLE)];
    if (items.length === 0) return;
    const first = items[0];
    const last = items[items.length - 1];
    const active = document.activeElement;
    if (event.shiftKey && (active === first || !panel.current.contains(active))) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && (active === last || !panel.current.contains(active))) {
      event.preventDefault();
      first.focus();
    }
  }

  return createPortal(
    <div data-confirm-dialog="" className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div
        aria-hidden="true"
        className="absolute inset-0 bg-canvas/80 backdrop-blur-sm"
        onClick={() => { if (!busy) onCancel(); }}
      />
      <div
        ref={panel}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={bodyId}
        aria-busy={busy || undefined}
        onKeyDown={onKeyDown}
        className="relative w-full max-w-md space-y-4 rounded-card border border-line-strong bg-surface-raised p-5 shadow-2xl"
      >
        <h2 id={titleId} className="break-words font-display text-xl [overflow-wrap:anywhere]">{title}</h2>
        <div id={bodyId} className="space-y-2 text-sm text-ink-muted">{children}</div>
        <ErrorNotice error={error} />
        <div className="flex flex-wrap justify-end gap-2">
          <Button ref={cancel} variant="secondary" disabled={busy} onClick={onCancel}>
            {cancelLabel}
          </Button>
          <Button variant={variant} loading={busy} loadingText={busyLabel} onClick={onConfirm}>
            {confirmLabel}
          </Button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
