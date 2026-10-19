# Multiplayer lobby, party controls and spectators (EDG-050)

Frontend for multiplayer M4, on top of EDG-046 (membership), EDG-047 (channel) and EDG-048 (spotlight).

## Screens

| Route | Who | What |
| --- | --- | --- |
| `/play/:sessionId/table` | any member | The lobby: presence, roles, seated heroes, whose turn it is, next-up order, connection state. |
| `/join#token=...` | signed-in invitee | Accept an invite, choosing an owned hero for a player seat. |
| `/play/:sessionId` | any member | Play screen. Shows "Your turn", a waiting banner naming who is acting, or the spectator view. |

Owner controls (lobby only): create and revoke invites (the link is shown once, with a copy button), change a member between player and spectator, remove a member, set turn mode and idle timeout, hand over or release the spotlight. Players and spectators can leave the campaign. Every control calls an existing EDG-046/048 route; the server stays authoritative and the UI only avoids offering what it would refuse.

## Live state

`useSessionLobby` (`frontend/src/components/lobby/`) reads the roster once over REST, then folds the EDG-047 channel through the pure `tableReducer`. Ordering, duplicate drop and `Last-Event-ID` resume are the channel client's job (`api/sessionChannel.ts`); the hook opens the channel after the roster read settles, so "is this turn mine" is known before the first record. A spotlight event older than the state already held is ignored; a snapshot replaces presence and spotlight. A seat change or a present member missing from the roster triggers a roster re-read. `closed: access_revoked` (or a 404 roster) ends the subscription and shows "You no longer have access". The hook aborts the stream on unmount.

The play screen re-reads committed history, scene, combat and the journal when another member's turn commits (`turn.finished`), deferring while the viewer's own turn is running. The viewer's own turn already streamed in and is not re-read.

## Added route

`GET /api/sessions/{id}/lobby` (any member; non-members get the session 404). Returns the viewer's role and seat plus each person's display name, role and the hero seated at this session. No emails, no invites. Owners keep `GET /campaigns/{id}/members` for the management view.

## Invite link

The token travels in the URL fragment (`#token=`), never the query, so it does not reach server logs or referrers. `/join` reads it once, keeps it in memory and removes it from the address bar.
