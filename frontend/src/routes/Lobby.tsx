import { useParams } from "react-router-dom";

import { LobbyView } from "../components/lobby/LobbyView";
import { ButtonLink } from "../components/ui/Button";
import { EmptyState } from "../components/ui/EmptyState";
import { Page } from "../components/ui/Page";

/** `/play/:sessionId/table`: the multiplayer lobby for one session (EDG-050). */
export default function Lobby() {
  const { sessionId: param } = useParams<{ sessionId: string }>();
  const sessionId = Number(param);
  if (!Number.isInteger(sessionId) || sessionId < 1) {
    return (
      <Page width="md">
        <EmptyState title="This table doesn't exist." action={<ButtonLink to="/campaigns">Back to campaigns</ButtonLink>}>
          The link may be wrong.
        </EmptyState>
      </Page>
    );
  }
  // Remount per session so presence, the subscription and the roster reset cleanly.
  return (
    <Page width="md">
      <LobbyView key={sessionId} sessionId={sessionId} />
    </Page>
  );
}
