import { useEffect, useRef, type RefObject } from "react";

const FOCUSABLE = [
  "a[href]", "button:not([disabled])", "input:not([disabled])", "select:not([disabled])", "summary",
  "textarea:not([disabled])", "[tabindex]:not([tabindex='-1'])",
].join(",");

function focusableWithin(container: HTMLElement): HTMLElement[] {
  return [...container.querySelectorAll<HTMLElement>(FOCUSABLE)].filter((element) => {
    if (element.closest("[hidden], [aria-hidden='true']")) return false;
    const closedDetails = element.closest("details:not([open])");
    if (closedDetails && !(element instanceof HTMLElement && element.tagName === "SUMMARY" && element.parentElement === closedDetails)) return false;
    const style = window.getComputedStyle(element);
    return style.display !== "none" && style.visibility !== "hidden" && element.getAttribute("aria-hidden") !== "true";
  });
}

/** Escape closes a tool and focus returns to its opener; narrow-screen dialogs also contain focus. */
export function useModalFocus(
  open: boolean,
  modal: boolean,
  onClose: () => void,
  containerRef: RefObject<HTMLElement | null>,
  findOpener?: () => HTMLElement | null,
) {
  const openerRef = useRef<HTMLElement | null>(null);
  useEffect(() => {
    if (!open) {
      openerRef.current = null;
      return;
    }
    // Expanding a side panel into a modal must preserve its original toolbar opener.
    if (!openerRef.current?.isConnected) {
      openerRef.current = findOpener?.() ?? (document.activeElement instanceof HTMLElement ? document.activeElement : null);
    }
    const previous = openerRef.current;
    const container = containerRef.current;
    let focusFrame: number | null = null;
    if (modal) {
      focusFrame = requestAnimationFrame(() => {
        if (!container) return;
        (focusableWithin(container)[0] ?? container).focus();
      });
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        // Escape cancels an IME composition, and the composer beside a nonmodal sidebar keeps its own Escape.
        if (event.isComposing || (!modal && !container?.contains(event.target as Node))) return;
        event.preventDefault();
        onClose();
        return;
      }
      if (!modal || event.key !== "Tab" || !container) return;
      const focusable = focusableWithin(container);
      if (!focusable.length) {
        event.preventDefault();
        container.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && (document.activeElement === first || !container.contains(document.activeElement))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || !container.contains(document.activeElement))) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      if (focusFrame !== null) cancelAnimationFrame(focusFrame);
      // A narration link may have deliberately moved focus outside the closing tool.
      const current = document.activeElement;
      const target = previous?.isConnected ? previous : findOpener?.();
      if (target?.isConnected && (!current || current === document.body || container?.contains(current))) target.focus();
    };
  }, [open, modal, onClose, containerRef, findOpener]);
}
