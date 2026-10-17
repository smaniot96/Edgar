// @vitest-environment jsdom
/**
 * Mounted useTurn lifecycle: real hook, real SSE parser, fake transport. These cover what the
 * committed transcript shows after an interrupted, replaced or unmounted stream.
 */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { installFakeApi, sseResponse, type FakeCall } from "../../test/fakeApi";
import { TURN_WATCHDOG_MS, useTurn, type UseTurnOptions } from "./useTurn";

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(() => root.unmount());
  container.remove();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function Harness({ sessionId, options, input = "Open the door", draftRevision = 4 }: {
  sessionId: number;
  options: UseTurnOptions;
  input?: string;
  draftRevision?: number;
}) {
  const turn = useTurn(sessionId, options);
  return (
    <>
      <button onClick={() => void turn.send(input, draftRevision)}>Send</button>
      <button onClick={() => turn.stop()}>Stop</button>
      {turn.state.pending ? <p data-role="pending">{turn.state.pending.text}</p> : null}
      <ul>
        {turn.state.lines.map((line) => (
          <li key={line.id} data-role={line.role} data-uncertain={line.role === "error" && line.uncertain ? "yes" : "no"}>
            {line.content}
          </li>
        ))}
      </ul>
    </>
  );
}

function button(label: string): HTMLButtonElement {
  return Array.from(container.querySelectorAll("button")).find((item) => item.textContent === label)!;
}

function lines(role: string): string[] {
  return Array.from(container.querySelectorAll<HTMLElement>(`li[data-role="${role}"]`)).map((el) => el.textContent ?? "");
}

function flush() {
  return act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
}

it("keeps the partial narration of a stream that errors after tokens and never acknowledges the draft", async () => {
  installFakeApi([[/^POST \/api\/sessions\/1\/turn\/stream$/, ({ signal }: FakeCall) => sseResponse([
    ["status", { stage: "narrating" }],
    ["token", { text: "The lantern gutters as " }],
    ["error", { code: "provider_unavailable" }],
  ], signal)]]);
  const options: UseTurnOptions = { debug: false, onAcknowledged: vi.fn(), onFailed: vi.fn(), onResult: vi.fn() };
  await act(() => root.render(<Harness sessionId={1} options={options} />));
  await act(() => button("Send").click());
  await flush();

  expect(lines("dm")).toEqual(["The lantern gutters as "]);
  const errors = lines("error");
  expect(errors).toHaveLength(1);
  expect(errors[0]).toContain("Check save status before retrying.");
  expect(container.querySelector('li[data-role="error"]')?.getAttribute("data-uncertain")).toBe("yes");
  // The server never confirmed a committed turn, so the draft must not be cleared.
  expect(options.onAcknowledged).not.toHaveBeenCalled();
  expect(options.onResult).not.toHaveBeenCalled();
  expect(options.onFailed).toHaveBeenCalledExactlyOnceWith("Open the door", 4);
});

it("replaces provisional tokens with the authoritative done narration as one committed DM line", async () => {
  installFakeApi([[/^POST \/api\/sessions\/1\/turn\/stream$/, ({ signal }: FakeCall) => sseResponse([
    ["token", { text: "Provisional guess about a hall" }],
    ["token", { text: " behind the door" }],
    ["done", { narration: "The door opens onto a cold hall.", current_scene_id: "hall" }],
  ], signal)]]);
  const options: UseTurnOptions = { debug: false, onAcknowledged: vi.fn(), onResult: vi.fn() };
  await act(() => root.render(<Harness sessionId={1} options={options} />));
  await act(() => button("Send").click());
  await flush();

  expect(lines("dm")).toEqual(["The door opens onto a cold hall."]);
  expect(container.textContent).not.toContain("Provisional guess");
  expect(options.onResult).toHaveBeenCalledOnce();
  expect(options.onAcknowledged).toHaveBeenCalledExactlyOnceWith(4, expect.any(String));
});

it("aborts a stalled stream on unmount, commits nothing, and its watchdog never reports a failure", async () => {
  vi.useFakeTimers();
  const { calls } = installFakeApi([[/^POST \/api\/sessions\/1\/turn\/stream$/, ({ signal }: FakeCall) => sseResponse([
    ["token", { text: "Partial narration" }],
  ], signal, true)]]);
  const options: UseTurnOptions = { debug: false, onAcknowledged: vi.fn(), onFailed: vi.fn(), onResult: vi.fn(), onSettled: vi.fn() };
  await act(() => root.render(<Harness sessionId={1} options={options} />));
  await act(() => button("Send").click());
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(container.querySelector('[data-role="pending"]')?.textContent).toBe("Partial narration");
  expect(lines("dm")).toEqual([]);

  const stream = calls.find((c) => c.method === "POST")!;
  expect(stream.signal?.aborted).toBe(false);
  await act(() => root.unmount());
  expect(stream.signal?.aborted).toBe(true);

  await act(async () => { await vi.advanceTimersByTimeAsync(TURN_WATCHDOG_MS + 1_000); });
  expect(options.onResult).not.toHaveBeenCalled();
  expect(options.onAcknowledged).not.toHaveBeenCalled();
  expect(options.onFailed).not.toHaveBeenCalled();
  expect(options.onSettled).not.toHaveBeenCalled();
  // Recreate the root so afterEach's unmount is a valid no-op.
  root = createRoot(container);
});

it("stops a stalled stream on the user's request, keeps the partial narration and asks for a save check", async () => {
  installFakeApi([[/^POST \/api\/sessions\/1\/turn\/stream$/, ({ signal }: FakeCall) => sseResponse([
    ["token", { text: "The stair groans under" }],
  ], signal, true)]]);
  const options: UseTurnOptions = { debug: false, onFailed: vi.fn(), onAcknowledged: vi.fn() };
  await act(() => root.render(<Harness sessionId={1} options={options} />));
  await act(() => button("Send").click());
  await flush();
  expect(container.querySelector('[data-role="pending"]')?.textContent).toBe("The stair groans under");

  await act(() => button("Stop").click());
  await flush();
  expect(container.textContent).toContain("You stopped the DM. Check save status before retrying.");
  expect(lines("dm")).toEqual(["The stair groans under"]);
  expect(options.onFailed).toHaveBeenCalledExactlyOnceWith("Open the door", 4);
  expect(options.onAcknowledged).not.toHaveBeenCalled();
});

it("reports a watchdog timeout once the server sends nothing for the full window", async () => {
  vi.useFakeTimers();
  installFakeApi([[/^POST \/api\/sessions\/1\/turn\/stream$/, ({ signal }: FakeCall) => sseResponse([], signal, true)]]);
  const options: UseTurnOptions = { debug: false, onFailed: vi.fn() };
  await act(() => root.render(<Harness sessionId={1} options={options} />));
  await act(() => button("Send").click());
  await act(async () => { await vi.advanceTimersByTimeAsync(TURN_WATCHDOG_MS - 1_000); });
  expect(options.onFailed).not.toHaveBeenCalled();
  await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
  expect(container.textContent).toContain("The DM didn't respond for 90 seconds.");
  expect(options.onFailed).toHaveBeenCalledExactlyOnceWith("Open the door", 4);
});
