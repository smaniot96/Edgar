import { Link } from "react-router-dom";

import { listCampaignCharacters } from "../../api/client";
import { EmptyState, LoadingText } from "../../components/ui/EmptyState";
import { ErrorNotice } from "../../components/ui/ErrorNotice";
import { cardClasses, linkClasses } from "../../components/ui/styles";
import { useMode } from "../../dev/ModeContext";
import { cn } from "../../lib/cn";
import { useQuery } from "../../lib/useQuery";

function HpBar({ current, max }: { current: number; max: number }) {
  const ratio = max > 0 ? Math.max(0, Math.min(1, current / max)) : 0;
  return (
    <div
      role="meter"
      aria-label="Hit points"
      aria-valuemin={0}
      aria-valuemax={max}
      aria-valuenow={current}
      className="mt-1.5 h-1.5 w-32 overflow-hidden rounded-full bg-surface-raised"
    >
      <div
        className={cn(
          "h-full rounded-full",
          ratio <= 0.25 ? "bg-danger" : ratio <= 0.5 ? "bg-ember-500" : "bg-success",
        )}
        style={{ width: `${ratio * 100}%` }}
      />
    </div>
  );
}

export default function CharactersTab({ campaignId }: { campaignId: number }) {
  const { isDev } = useMode();
  const chars = useQuery(`campaign-characters:${campaignId}`, (s) =>
    listCampaignCharacters(campaignId, s),
  );

  return (
    <div className="space-y-4">
      <p className="text-sm text-ink-muted">
        Characters currently adventuring in this campaign. Characters join when you start a
        session with them; manage your roster on the{" "}
        <Link to="/characters" className={linkClasses}>
          Characters
        </Link>{" "}
        page.
      </p>
      {chars.error && chars.data === undefined ? (
        <ErrorNotice error={chars.error} onRetry={chars.refetch} />
      ) : chars.data === undefined ? (
        <LoadingText />
      ) : chars.data.length === 0 ? (
        <EmptyState title="No characters here yet">
          Start a session with a character to bring them into this campaign.
        </EmptyState>
      ) : (
        <ul className="space-y-2">
          {chars.data.map((c) => (
            <li key={c.assignment_id} className={cn(cardClasses, "px-4 py-3")}>
              <p className="text-sm font-medium">
                {c.name}
                {isDev ? (
                  <span className="ml-2 font-mono text-xs text-ink-subtle">
                    char #{c.character_id} · assignment #{c.assignment_id}
                  </span>
                ) : null}
              </p>
              <p className="text-xs text-ink-muted">
                Level {c.level} {c.character_class} · HP {c.hp_current}/{c.hp_max}
              </p>
              <HpBar current={c.hp_current} max={c.hp_max} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
