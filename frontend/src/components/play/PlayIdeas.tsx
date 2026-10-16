import type { JournalRead } from "../journal/types";
import { Button } from "../ui/Button";

/** Optional examples use only the same earned projection as the Journal. */
export function PlayIdeas({ data, hasDraft, onAppend }: {
  data: JournalRead | null;
  hasDraft: boolean;
  onAppend: (idea: string) => void;
}) {
  const ideas: string[] = [];
  if (data?.current_place) ideas.push(`I inspect the surroundings at ${data.current_place.title}.`);
  const person = data?.entries.find((entry) => entry.category === "person");
  if (person) ideas.push(`I ask ${person.title} about what we have already learned.`);
  const promise = data?.entries.find((entry) => entry.category === "promise" && entry.status === "active" && entry.origin.actor);
  if (promise) ideas.push(`I consider how to keep my promise to ${promise.origin.actor}.`);
  else if (data?.active_goal) ideas.push(`I look for a safe next step toward my goal: ${data.active_goal.title}.`);

  return (
    <details className="shrink-0 border-t border-line bg-canvas/80 px-3 py-2 sm:px-4">
      <summary className="mx-auto min-h-11 max-w-3xl cursor-pointer py-2 text-sm font-medium text-ink-muted focus-visible:outline focus-visible:outline-2 focus-visible:outline-ember-400">Ideas</summary>
      <div className="mx-auto mt-2 max-h-44 max-w-3xl space-y-2 overflow-y-auto text-sm">
        <p className="text-xs text-ink-subtle">Optional examples from what you know. You can write any action you want.</p>
        {ideas.length ? ideas.map((idea) => (
          <div key={idea} className="flex items-start justify-between gap-3">
            <p className="min-w-0 break-words text-ink-muted">{idea}</p>
            <Button type="button" variant="secondary" size="sm" className="min-h-11 shrink-0" onClick={() => onAppend(idea)}>
              {hasDraft ? "Append to draft" : "Add to draft"}
            </Button>
          </div>
        )) : <p className="text-ink-muted">There is not enough recorded context for ideas yet. Your own action is always welcome.</p>}
      </div>
    </details>
  );
}
