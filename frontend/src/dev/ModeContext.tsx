import { createContext, useCallback, useContext, useMemo, useState } from "react";
import type { ReactNode } from "react";

export type Mode = "user" | "dev";

const STORAGE_KEY = "edgar_mode";

function readInitialMode(): Mode {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    return v === "dev" ? "dev" : "user";
  } catch {
    return "user";
  }
}

interface ModeContextValue {
  mode: Mode;
  isDev: boolean;
  setMode: (mode: Mode) => void;
  toggleMode: () => void;
}

const ModeContext = createContext<ModeContextValue | null>(null);

export function ModeProvider({ children }: { children: ReactNode }) {
  const [mode, setModeState] = useState<Mode>(readInitialMode);

  const setMode = useCallback((next: Mode) => {
    setModeState(next);
    try {
      localStorage.setItem(STORAGE_KEY, next);
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

export function useMode(): ModeContextValue {
  const ctx = useContext(ModeContext);
  if (!ctx) throw new Error("useMode must be used within a ModeProvider");
  return ctx;
}
