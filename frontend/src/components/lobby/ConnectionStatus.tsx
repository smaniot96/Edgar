import type { ChannelStatus } from "../../api/sessionChannel";
import { Pill } from "../Pill";

const COPY: Record<ChannelStatus | "idle", { label: string; tone: "success" | "warning" | "danger" | "neutral"; hint: string }> = {
  idle: { label: "Not connected", tone: "neutral", hint: "Live updates have not started." },
  connecting: { label: "Connecting…", tone: "neutral", hint: "Opening the live table." },
  live: { label: "Live", tone: "success", hint: "You will see changes as they happen." },
  reconnecting: { label: "Reconnecting…", tone: "warning", hint: "Connection lost. Trying again; nothing is missed once it returns." },
  closed: { label: "Disconnected", tone: "danger", hint: "This table can no longer be followed live." },
};

export function ConnectionStatus({ status }: { status: ChannelStatus | "idle" }) {
  const copy = COPY[status];
  return (
    <span title={copy.hint}>
      <Pill tone={copy.tone}>
        <span aria-hidden="true" className="inline-block h-1.5 w-1.5 rounded-full bg-current" />
        <span data-testid="connection-status">{copy.label}</span>
      </Pill>
    </span>
  );
}
