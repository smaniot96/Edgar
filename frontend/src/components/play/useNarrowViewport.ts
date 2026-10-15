import { useEffect, useState } from "react";

const NARROW_QUERY = "(max-width: 1023px)";

export function useNarrowViewport(): boolean {
  const [narrow, setNarrow] = useState(() =>
    typeof window !== "undefined" && window.matchMedia?.(NARROW_QUERY).matches === true,
  );

  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const query = window.matchMedia(NARROW_QUERY);
    const update = () => setNarrow(query.matches);
    update();
    query.addEventListener?.("change", update);
    return () => query.removeEventListener?.("change", update);
  }, []);

  return narrow;
}
