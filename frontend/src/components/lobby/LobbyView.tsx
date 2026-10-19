import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { leaveCampaign } from "../../api/client";
import { ButtonLink, Button } from "../ui/Button";
import { EmptyState, LoadingText } from "../ui/EmptyState";
import { ErrorNotice, OfflineState } from "../ui/ErrorNotice";
import { PageHeader } from "../ui/Page";
import { cardClasses } from "../ui/styles";
import { cn } from "../../lib/cn";
import { ConnectionStatus } from "./ConnectionStatus";
import { InvitePanel } from "./InvitePanel";
import { RosterPanel } from "./RosterPanel";
import { OwnerSpotlightControls, SpotlightPanel } from "./SpotlightPanel";
import { useSessionLobby } from "./useSessionLobby";

/**
 * The table lobby (EDG-050): presence, roles, seated heroes, whose turn it is and the next-up order,
 * live from the session channel. The owner gets management controls; players can leave; spectators
 * see the same table read-only. Every control is a real button or field, reachable by keyboard.
 */
export function LobbyView({ sessionId }: { sessionId: number }) {
  const navigate = useNavigate();
  const table = useSessionLobby(sessionId);
  const [leaving, setLeaving] = useState(false);
  const [leaveError, setLeaveError] = useState<unknown>(null);
  const { lobby } = table;

  if (table.revoked) {
    return (
      <EmptyState
        title="You no longer have access to this table"
        action={<ButtonLink to="/campaigns">Back to campaigns</ButtonLink>}
      >
        The owner may have removed you, or this session was deleted. Ask the owner for a new invite to rejoin.
      </EmptyState>
    );
  }
  if (!table.loaded) return <LoadingText>Setting the table…</LoadingText>;
  if (!lobby) {
    return <OfflineState error={table.lobbyError} onRetry={table.refresh} title="Couldn't load the table" />;
  }

  const { viewer } = lobby;
  const isOwner = viewer.role === "owner";
  const seated = table.roster.filter((row) => row.characterId !== null);
  const watching = viewer.role === "spectator";

  async function leave() {
    if (!lobby || !window.confirm("Leave this campaign? Your seat is freed and your characters are kept.")) return;
    setLeaving(true);
    setLeaveError(null);
    try {
      await leaveCampaign(lobby.campaign_id);
      navigate("/campaigns");
    } catch (err) {
      setLeaveError(err);
      setLeaving(false);
    }
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title="The table"
        description={
          isOwner
            ? "You run this table. Invite people, seat heroes and decide whose turn it is."
            : watching
              ? "You are a spectator here. You can follow everything but cannot act."
              : viewer.character_name
                ? `You are playing ${viewer.character_name}.`
                : "You have no hero seated yet."
        }
        actions={
          <>
            <ConnectionStatus status={table.status} />
            <ButtonLink to={`/play/${sessionId}`}>{watching ? "Watch the adventure" : "Open the adventure"}</ButtonLink>
          </>
        }
      />

      {table.status === "reconnecting" ? (
        <p role="status" className="rounded-control border border-warning/50 bg-warning-soft/60 px-3 py-2 text-sm text-warning">
          You are offline. This shows the last known table; it catches up on its own when the connection returns.
        </p>
      ) : null}
      {table.status === "closed" && !table.revoked ? (
        <p role="alert" className="rounded-control border border-danger-strong/60 bg-danger-soft/60 px-3 py-2 text-sm text-danger">
          Live updates stopped. Reload the page to follow the table again.
        </p>
      ) : null}
      {table.lobbyError ? <ErrorNotice error={table.lobbyError} onRetry={table.refresh} /> : null}

      <SpotlightPanel spotlight={table.spotlight} selfUserId={viewer.user_id} />
      <RosterPanel rows={table.roster} campaignId={lobby.campaign_id} canManage={isOwner} onChanged={table.refresh} />
      {isOwner ? (
        <>
          <OwnerSpotlightControls sessionId={sessionId} spotlight={table.spotlight} seated={seated} onChanged={table.refresh} />
          <InvitePanel campaignId={lobby.campaign_id} sessionId={sessionId} />
        </>
      ) : (
        <section aria-labelledby="you-title" className={cn(cardClasses, "space-y-3 p-4")}>
          <h2 id="you-title" className="font-display text-lg text-ink">Your place</h2>
          <p className="text-sm text-ink-muted">
            {watching
              ? "Spectators have no seat. Ask the owner to make you a player if you want to join the action."
              : viewer.character_name
                ? `${viewer.character_name} is seated at this table.`
                : "To take a seat, ask the owner for a table invite and open its link."}
          </p>
          <ErrorNotice error={leaveError} />
          <Button variant="danger" size="sm" loading={leaving} loadingText="Leaving…" onClick={() => void leave()}>
            Leave campaign
          </Button>
        </section>
      )}

      {/* One polite region for arrivals, departures and turn changes. */}
      <div role="status" aria-live="polite" aria-atomic="true" className="sr-only">{table.announcement}</div>
    </div>
  );
}
