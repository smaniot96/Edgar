import { useState } from "react";

import { resolveAdventureCoverUrl } from "../../lib/adventureCover";

export function AdventureCover({ coverImage, name }: { coverImage: string | null | undefined; name: string }) {
  const src = resolveAdventureCoverUrl(coverImage);
  const [failedSource, setFailedSource] = useState<string | null>(null);

  if (!src || failedSource === src) {
    const initial = name.replace(/^(the|a|an)\s+/i, "").trim().charAt(0).toUpperCase() || "E";
    return (
      <div
        aria-hidden
        className="relative flex h-32 w-full items-center justify-center overflow-hidden rounded-t-card bg-gradient-to-br from-ember-950/70 via-surface-sunken to-canvas"
      >
        <div
          className="absolute inset-0"
          style={{ background: "radial-gradient(60% 70% at 50% 110%, rgba(255,107,43,0.28), transparent 70%)" }}
        />
        <span className="relative font-display text-6xl font-bold text-ember-300/80">{initial}</span>
      </div>
    );
  }

  return (
    <img
      src={src}
      alt=""
      className="h-32 w-full rounded-t-card object-cover"
      onError={() => setFailedSource(src)}
    />
  );
}
