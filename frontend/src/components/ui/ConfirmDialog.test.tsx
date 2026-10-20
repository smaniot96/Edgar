// @vitest-environment jsdom
import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { ApiError } from "../../api/client";
import { ConfirmDialog } from "./ConfirmDialog";

let container: HTMLDivElement;
let root: Root;
const onConfirm = vi.fn();
const onCancel = vi.fn();

function Harness({ busy = false, error = null }: { busy?: boolean; error?: unknown }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button onClick={() => setOpen(true)}>Open</button>
      <ConfirmDialog
        open={open}
        title="Delete “Velmoor”?"
        confirmLabel="Delete campaign"
        busy={busy}
        error={error}
        onConfirm={onConfirm}
        onCancel={() => { onCancel(); setOpen(false); }}
      >
        <p>This permanently deletes the campaign “Velmoor” and its 2 saved sessions.</p>
      </ConfirmDialog>
    </>
  );
}

async function openDialog(props: { busy?: boolean; error?: unknown } = {}) {
  await act(async () => root.render(<Harness {...props} />));
  const opener = container.querySelector("button")!;
  opener.focus();
  await act(async () => opener.click());
  return { opener, dialog: document.querySelector<HTMLElement>("[role='alertdialog']")! };
}

function key(target: Element, init: KeyboardEventInit) {
  target.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init }));
}

function buttons(dialog: HTMLElement) {
  return [...dialog.querySelectorAll("button")];
}

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.clearAllMocks();
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});
afterEach(async () => {
  await act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

it("is a labelled modal alertdialog that names the consequence and focuses Cancel", async () => {
  const { dialog } = await openDialog();
  expect(dialog.getAttribute("aria-modal")).toBe("true");
  expect(document.getElementById(dialog.getAttribute("aria-labelledby")!)?.textContent).toBe("Delete “Velmoor”?");
  expect(document.getElementById(dialog.getAttribute("aria-describedby")!)?.textContent).toContain("its 2 saved sessions");
  expect(document.activeElement?.textContent).toBe("Cancel");
});

it("makes the page behind inert while open and restores it and focus on Escape", async () => {
  const { opener, dialog } = await openDialog();
  expect(container.inert).toBe(true);
  await act(async () => key(dialog, { key: "Escape" }));
  expect(onCancel).toHaveBeenCalledOnce();
  expect(document.querySelector("[role='alertdialog']")).toBeNull();
  expect(container.inert).toBe(false);
  expect(document.activeElement).toBe(opener);
});

it("keeps Tab focus inside the dialog", async () => {
  const { dialog } = await openDialog();
  const [cancel, confirm] = buttons(dialog);
  confirm.focus();
  key(dialog, { key: "Tab" });
  expect(document.activeElement).toBe(cancel);
  key(dialog, { key: "Tab", shiftKey: true });
  expect(document.activeElement).toBe(confirm);
});

it("confirms only from the confirm button", async () => {
  const { dialog } = await openDialog();
  await act(async () => buttons(dialog)[1].click());
  expect(onConfirm).toHaveBeenCalledOnce();
  expect(onCancel).not.toHaveBeenCalled();
});

it("cannot be dismissed while busy and shows a failure inside", async () => {
  const { dialog } = await openDialog({ busy: true, error: new ApiError(503, "") });
  await act(async () => key(dialog, { key: "Escape" }));
  expect(onCancel).not.toHaveBeenCalled();
  expect(buttons(dialog).every((b) => b.disabled)).toBe(true);
  expect(dialog.querySelector("[role='alert']")).not.toBeNull();
});
