import { useCallback, useMemo, useState } from "react";
import type { ReactNode } from "react";

import { MODE_STORAGE_KEY, ModeContext } from "./ModeContext";
import type { Mode, ModeContextValue } from "./ModeContext";

function readInitialMode(): Mode {
  try {
    return localStorage.getItem(MODE_STORAGE_KEY) === "dev" ? "dev" : "user";
  } catch {
    return "user";
  }
}

export function ModeProvider({ children }: { children: ReactNode }) {
  const [mode, setModeState] = useState<Mode>(readInitialMode);

  const setMode = useCallback((next: Mode) => {
    setModeState(next);
    try {
      localStorage.setItem(MODE_STORAGE_KEY, next);
    } catch {
      /* persistence is best-effort */
    }
  }, []);

  const toggleMode = useCallback(() => {
    setMode(mode === "dev" ? "user" : "dev");
  }, [mode, setMode]);

  const value = useMemo<ModeContextValue>(
    () => ({ mode, isDev: mode === "dev", setMode, toggleMode }),
    [mode, setMode, toggleMode],
  );

  return <ModeContext.Provider value={value}>{children}</ModeContext.Provider>;
}
