import { Link, NavLink, Outlet } from "react-router-dom";

import EdgarLogo from "./components/EdgarLogo";
import { useMode } from "./dev/ModeContext";
import { DevBadge, ModeToggle } from "./dev/ModeToggle";
import { cn } from "./lib/cn";

function navClass({ isActive }: { isActive: boolean }) {
  return cn("rounded px-2 py-1 hover:bg-[#222]", isActive && "bg-[#222] text-white");
}

export default function App() {
  const { isDev } = useMode();
  return (
    <div className="flex min-h-screen flex-col bg-[#101216] font-sans text-[#e6e6e6]">
      {/* Thin amber top border makes Developer mode obvious at a glance. */}
      {isDev ? <div className="h-0.5 w-full bg-amber-500" /> : null}
      <nav className="flex flex-wrap items-center gap-2 border-b border-[#222] px-4 py-2 text-sm">
        <Link to="/" className="mr-2 flex items-center" aria-label="Edgar home">
          <EdgarLogo variant="mark" className="h-7 w-auto" title="Edgar — home" />
        </Link>
        <NavLink to="/campaigns" className={navClass}>
          Campaigns
        </NavLink>
        <NavLink to="/characters" className={navClass}>
          Characters
        </NavLink>
        <NavLink to="/settings" className={navClass}>
          Settings
        </NavLink>
        <div className="ml-auto flex items-center gap-2">
          <DevBadge />
          <ModeToggle />
        </div>
      </nav>
      <div className="flex min-h-0 flex-1 flex-col">
        <Outlet />
      </div>
    </div>
  );
}
