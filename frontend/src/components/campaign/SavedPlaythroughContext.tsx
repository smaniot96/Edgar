import type { SessionRead } from "../../api/types";
import { currentThread } from "../journal/model";
import { useJournal } from "../journal/api";

/** Only session-earned journal orientation; no private adventure premise or scene IDs. */
export function SavedPlaythroughContext({ session, className = "mb-3" }: { session: SessionRead | null; className?: string }) {
  const journal = useJournal(session?.has_history && session.readiness?.history_allowed === true ? session.id : null);
  const thread = currentThread(journal.data);
  if (!journal.data?.current_place && !thread) return null;
  return (
    <dl aria-label="Saved adventure context" className={`${className} space-y-1 text-sm [overflow-wrap:anywhere]`}>
      {journal.data?.current_place ? <div><dt className="inline font-medium text-ink-subtle">Known place: </dt><dd className="inline text-ink-muted">{journal.data.current_place.title}</dd></div> : null}
      {thread ? <div><dt className="inline font-medium text-ink-subtle">{journal.data?.active_goal ? "Goal" : "Latest lead"}: </dt><dd className="inline text-ink-muted">{thread.title}</dd></div> : null}
    </dl>
  );
}
