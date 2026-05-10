import { NavLink, Outlet } from "react-router-dom";

import { cn } from "./lib/cn";

function navClass({ isActive }: { isActive: boolean }) {
  return cn("rounded px-2 py-1 hover:bg-[#222]", isActive && "bg-[#222] text-white");
}

export default function App() {
  return (
    <div className="flex min-h-screen flex-col bg-[#101216] font-sans text-[#e6e6e6]">
      <nav className="flex flex-wrap items-center gap-2 border-b border-[#222] px-4 py-2 text-sm">
        <span className="mr-2 font-semibold text-[#9ca3af]">Edgar</span>
        <NavLink to="/" end className={navClass}>
          Home
        </NavLink>
        <NavLink to="/new" className={navClass}>
          New adventure
        </NavLink>
        <NavLink to="/settings" className={navClass}>
          Settings
        </NavLink>
      </nav>
      <div className="flex min-h-0 flex-1 flex-col">
        <Outlet />
      </div>
    </div>
  );
}
