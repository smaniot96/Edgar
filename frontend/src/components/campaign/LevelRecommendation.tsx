import { recommendedLevels } from "../../lib/campaign";

export function LevelRecommendation({ range, heroes = [] }: {
  range?: string | null;
  heroes?: { name: string; level: number }[];
}) {
  const levels = recommendedLevels(range);
  if (!levels) return null;
  const mismatched = heroes.filter((hero) => hero.level < levels.min || hero.level > levels.max);
  return <div className="space-y-1 rounded-control border border-line bg-surface-raised p-3 text-sm">
    <p>Recommended level: {levels.min === levels.max ? levels.min : `${levels.min}–${levels.max}`}.</p>
    {mismatched.length > 0 ? <p role="status" className="text-warning">
      {mismatched.map((hero) => `${hero.name} (level ${hero.level})`).join(", ")} {mismatched.length === 1 ? "is" : "are"} outside this range. You can continue or choose a hero at the recommended level.
    </p> : null}
  </div>;
}
