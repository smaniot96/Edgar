import { Link, NavLink, Outlet } from "react-router-dom";

import EdgarLogo from "./components/EdgarLogo";
import { GearIcon } from "./components/ui/icons";
import { useMode } from "./dev/ModeContext";
import { ModeToggle } from "./dev/ModeToggle";
import { cn } from "./lib/cn";

function navClass({ isActive }: { isActive: boolean }) {
  return cn(
    "inline-flex h-8 items-center gap-1.5 rounded-control px-2.5 text-sm font-medium transition-colors",
    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-400",
    isActive
      ? "bg-surface-raised text-ink"
      : "text-ink-muted hover:bg-surface-raised/60 hover:text-ink",
  );
}

/**
 * App shell: a fixed-height (h-dvh) column — nav on top, routed page in a `flex-1 min-h-0`
 * region. Pages own their scrolling (see `Page`), which lets Play pin its composer.
 */
export default function App() {
  const { isDev } = useMode();
  return (
    <div className="flex h-dvh flex-col overflow-hidden bg-canvas font-sans text-ink">
      <header
        className={cn(
          "shrink-0 border-b border-line bg-canvas/95",
          // Thin amber top border makes Developer mode obvious at a glance.
          isDev && "border-t-2 border-t-ember-500",
        )}
      >
        <nav
          aria-label="Main"
          className="mx-auto flex h-12 w-full items-center gap-1 px-2 sm:gap-2 sm:px-4"
        >
          <Link
            to="/"
            className="mr-1 flex w-16 shrink-0 items-center rounded-control focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ember-400 sm:mr-3 sm:w-[4.5rem]"
          >
            <EdgarLogo variant="mark" title="Edgar — home" />
          </Link>
          <NavLink to="/campaigns" className={navClass}>
            Campaigns
          </NavLink>
          <NavLink to="/characters" className={navClass}>
            Characters
          </NavLink>
          <div className="ml-auto flex shrink-0 items-center gap-1">
            <NavLink to="/settings" className={navClass} title="Settings">
              <GearIcon className="h-4 w-4" />
              <span className="sr-only sm:not-sr-only">Settings</span>
            </NavLink>
            <ModeToggle />
          </div>
        </nav>
      </header>
      {/* Not <main>: routed pages (e.g. Play) render their own landmark. */}
      <div className="flex min-h-0 flex-1 flex-col">
        <Outlet />
      </div>
    </div>
  );
}
