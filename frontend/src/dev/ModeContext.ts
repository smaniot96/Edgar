import { createContext, useContext } from "react";

export type Mode = "user" | "dev";

export interface ModeContextValue {
  mode: Mode;
  isDev: boolean;
  setMode: (mode: Mode) => void;
  toggleMode: () => void;
}

export const MODE_STORAGE_KEY = "edgar_mode";

export const ModeContext = createContext<ModeContextValue | null>(null);

/** User/Developer mode. Developer mode reveals ids, raw errors and the per-turn debug panel. */
export function useMode(): ModeContextValue {
  const ctx = useContext(ModeContext);
  if (!ctx) throw new Error("useMode must be used within a ModeProvider");
  return ctx;
}
