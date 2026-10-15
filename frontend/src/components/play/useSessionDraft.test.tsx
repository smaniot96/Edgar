// @vitest-environment jsdom
import { act, useEffect } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useSessionDraft } from "./useSessionDraft";

let root: Root;
let container: HTMLDivElement;
let draft: ReturnType<typeof useSessionDraft>;
function Harness({ sessionId }: { sessionId: number }) {
  const sessionDraft = useSessionDraft(sessionId);
  useEffect(() => { draft = sessionDraft; }, [sessionDraft]);
  return <textarea aria-label="Draft" value={sessionDraft.draft} onChange={(event) => sessionDraft.edit(event.target.value)} />;
}
beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  localStorage.clear(); container = document.createElement("div"); document.body.append(container);
  root = createRoot(container);
});
afterEach(async () => { await act(() => root.unmount()); container.remove(); vi.unstubAllGlobals(); });

it("preserves a multiline draft through reload and keeps it out of another session", async () => {
  await act(() => root.render(<Harness key={1} sessionId={1} />));
  await act(() => draft.edit("I keep the note dry.\nThen I ask Mara about the lens."));
  await act(() => root.render(<Harness key={2} sessionId={2} />));
  expect(draft.draft).toBe("");
  await act(() => root.render(<Harness key="reload-1" sessionId={1} />));
  expect(draft.draft).toBe("I keep the note dry.\nThen I ask Mara about the lens.");
});

it("clears only the acknowledged revision and preserves edits typed while sending", async () => {
  await act(() => root.render(<Harness key={1} sessionId={1} />));
  await act(() => draft.edit("First action"));
  const sentRevision = draft.revision;
  await act(() => draft.beginRequest({ requestId: "stable-1", input: "First action", draftRevision: sentRevision }));
  await act(() => draft.edit("My next action"));
  await act(() => { draft.clearIfRevision(sentRevision); draft.clearRequest("stable-1"); });
  expect(draft.draft).toBe("My next action");
  expect(draft.pendingRequest).toBeNull();
  await act(() => root.render(<Harness key="reload-1" sessionId={1} />));
  expect(draft.draft).toBe("My next action");
  const latestRevision = draft.revision;
  await act(() => draft.beginRequest({ requestId: "stable-2", input: "My next action", draftRevision: latestRevision }));
  await act(() => { draft.clearIfRevision(latestRevision); draft.clearRequest("stable-2"); });
  expect(draft.draft).toBe("");
  await act(() => root.render(<Harness key="reload-cleared" sessionId={1} />));
  expect(draft.draft).toBe(""); expect(draft.pendingRequest).toBeNull();
});

it("retains the request identity through reload until its own acknowledgment", async () => {
  await act(() => root.render(<Harness key={1} sessionId={1} />));
  await act(() => draft.edit("Open the door"));
  const request = { requestId: "stable-3", input: draft.draft, draftRevision: draft.revision };
  await act(() => draft.beginRequest(request));
  await act(() => root.render(<Harness key="reload-pending" sessionId={1} />));
  expect(draft.pendingRequest).toEqual(request);
  await act(() => draft.clearRequest("unrelated-id"));
  expect(draft.pendingRequest).toEqual(request);
});
